#!/usr/bin/env python3
"""Detektor orang MULTI-KAMERA ringan (alur TERPISAH dari pipeline taman penuh).

SATU service, banyak stream: supervisor membaca cameras.json dan menjalankan satu
worker-thread per kamera peran `garasi-ringan`. Menambah kamera = tambah entri di
cameras.json (dari viewer/Telegram) -> supervisor start worker-nya LIVE, tanpa
systemctl. Model YOLO DIBAGI semua worker (satu load VRAM; predict di-serialize).

Tiap worker: baca stream go2rtc (nama, bukan URL berkredensial), deteksi kelas
`person` (yolo11s) + tripwire garis (bila ada) -> event `garasi` (tag camera) ke
cctv.db. Deteksi jalan 24/7 (seperti taman); `jadwal` = gerbang NOTIF saja: di
dalam jendela notify=1 (bot kirim), di luar notify=0 (tetap tercatat+berklip di
viewer, senyap). MotionGate tetap melewati inferensi saat scene DIAM (hemat GPU).
Jadwal/enabled/garis dibaca LIVE (mtime). Rekam ⊥ notif.

    uv run --env-file .env pipeline/run_garasi.py --cameras-file cameras.json

Logika murni (dalam_jendela, Debounce) teruji -> test_run_garasi.py.
"""
import os
import json
import time
import signal
import argparse
import threading
from pathlib import Path

from db import EventWriter                      # kontrak notif SAMA dgn pipeline inti


# ══ Logika murni (teruji) ═══════════════════════════════════════════════════════
def _hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_window(frm, to, minutes):
    """menit-hari `minutes` di [frm,to)? Dukung lewat tengah malam (frm>to)."""
    a, b = _hhmm(frm), _hhmm(to)
    if a <= b:
        return a <= minutes < b
    return minutes >= a or minutes < b


def dalam_jendela(now, jadwal):
    """True bila `now` (epoch lokal) ada di salah satu jendela AKTIF. jadwal kosong
    -> True (FAIL-SAFE: kalau jadwal belum diisi, garasi tetap mendeteksi)."""
    if not jadwal:
        return True
    lt = time.localtime(now)
    menit = lt.tm_hour * 60 + lt.tm_min
    for w in jadwal:
        if w.get("aktif", True) and _in_window(w.get("from", "00:00"), w.get("to", "24:00"), menit):
            return True
    return False


class Debounce:
    """Anti false-positive + anti-spam. Butuh `need_frames` inferensi beruntun
    ADA-person sebelum melahirkan event; lalu `cooldown_s` jeda antar-event.
    Murni-state (mudah diuji). on_frame(ada, t) -> True bila event harus dilahirkan."""

    def __init__(self, need_frames=3, cooldown_s=60):
        self.need = need_frames
        self.cooldown = cooldown_s
        self.streak = 0
        self.last_fire = -1e18

    def on_frame(self, ada, t):
        if not ada:
            self.streak = 0
            return False
        self.streak += 1
        if self.streak >= self.need and (t - self.last_fire) >= self.cooldown:
            self.last_fire = t                 # streak TETAP tinggi -> re-fire tiap cooldown selama hadir
            return True
        return False


def pilih_kamera(cfg, peran="garasi-ringan"):
    """Kamera enabled pertama dgn peran cocok dari cameras.json, atau None."""
    for k in cfg.get("kamera", []):
        if k.get("peran") == peran and k.get("enabled", True):
            return k
    return None


def stream_url(nama):
    return f"rtsp://localhost:8554/{nama}"      # go2rtc menyimpan kredensial; di sini cuma nama


# ══ I/O ═════════════════════════════════════════════════════════════════════════
class ConfigWatcher:
    """Baca cameras.json; reload hanya saat mtime berubah (edit viewer -> live).
    File rusak/hilang -> pertahankan config terakhir (jangan matikan loop)."""

    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.cfg = {"kamera": []}
        self.reload_if_changed()

    def reload_if_changed(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return self.cfg
        if m != self.mtime:
            self.mtime = m
            try:
                self.cfg = json.loads(Path(self.path).read_text())
                print(f"[CONFIG] cameras.json dimuat ({len(self.cfg.get('kamera', []))} kamera)", flush=True)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[CONFIG] gagal baca {self.path} ({e!r}) -> pakai config lama", flush=True)
        return self.cfg


class FrameSource:
    """RTSP -> frame (cv2, TCP, buffer 1 = frame terbaru). Stream putus -> generator
    habis -> proses exit -> systemd restart."""

    def __init__(self, sumber):
        import cv2
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.cap = cv2.VideoCapture(sumber)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            raise RuntimeError(f"Gagal buka sumber: {sumber}")

    def frames(self):
        while True:
            ok, frame = self.cap.read()
            if not ok:
                return
            yield time.time(), frame

    def release(self):
        self.cap.release()


def _dalam_rect(cx, cy, rects):
    """(cx,cy) fraksi [0..1] jatuh di salah satu rect [x1,y1,x2,y2] fraksi?"""
    for x1, y1, x2, y2 in rects:
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return True
    return False


def ada_person(model, frame, conf, abaikan=()):
    """True bila ada box `person` yang PUSATNYA di luar semua zona `abaikan`
    (fraksi). abaikan = kotak tetap tempat YOLO sering phantom (mis. tumpukan
    barang pojok yg keliru dibaca orang di IR malam)."""
    for r in model.predict(frame, classes=[0], conf=conf, verbose=False):
        if r.boxes is None:
            continue
        h, w = r.orig_shape
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
            if not _dalam_rect(cx, cy, abaikan):
                return True
    return False


class MotionGate:
    """Gerbang GERAK murah SEBELUM YOLO. Frame-diff grayscale (kecil+blur) vs frame
    sampel sebelumnya; 'ada gerak' bila BLOB-berubah TERBESAR (komponen tersambung)
    >= min_area. Pakai blob terbesar, BUKAN jumlah piksel: riak air/grain IR =
    banyak titik kecil tersebar -> ditolak; orang berjalan = satu blob besar -> lolos.

    Alasan: malam-IR, scene DIAM sering bikin YOLO memunculkan phantom-person (false
    positive, mis. pojok bertumpuk) & buang GPU. Scene diam -> tak ada gerak -> YOLO
    dilewati. `abaikan` (kotak fraksi) di-nol-kan dari diff -> area kolam/air yg
    sering beriak tak membuka gerbang. Kecilkan+blur meredam grain. Warmup: frame
    pertama -> 'diam'. Butuh cv2."""

    def __init__(self, min_area_frac=0.0025, delta_thresh=25, size=(320, 180),
                 blur=21, abaikan=()):
        self.min_area_frac = min_area_frac
        self.delta_thresh = delta_thresh
        self.size = size
        self.blur = blur | 1                       # ksize ganjil
        self.abaikan = abaikan
        self.prev = None
        self._mask = None                          # cache mask piksel (0 di zona abaikan)

    def _prep(self, frame):
        import cv2
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, self.size)
        return cv2.GaussianBlur(g, (self.blur, self.blur), 0)

    def _mask_for(self):
        import numpy as np
        if self._mask is None:
            W, H = self.size
            m = np.ones((H, W), np.uint8)
            for x1, y1, x2, y2 in self.abaikan:
                m[int(y1 * H):int(y2 * H), int(x1 * W):int(x2 * W)] = 0
            self._mask = m
        return self._mask

    def ada_gerak(self, frame):
        """(ada_gerak: bool, fraksi_blob_terbesar: float). Update baseline internal."""
        import cv2
        import numpy as np
        g = self._prep(frame)
        if self.prev is None:
            self.prev = g
            return False, 0.0
        delta = cv2.absdiff(self.prev, g)
        self.prev = g
        _, th = cv2.threshold(delta, self.delta_thresh, 255, cv2.THRESH_BINARY)
        if self.abaikan:
            th = th * self._mask_for()             # buang gerak di zona abaikan (air/phantom)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))  # buang speckle
        nb, _, stats, _ = cv2.connectedComponentsWithStats(th, connectivity=8)
        blob = max((stats[i, cv2.CC_STAT_AREA] for i in range(1, nb)), default=0)
        frac = blob / float(self.size[0] * self.size[1])
        return frac >= self.min_area_frac, frac


# ══ Tripwire: penyeberangan GARIS berarah oleh titik-kaki (murni, teruji) ═══════
def _cross_sign(a, b, p):
    """Sisi titik p relatif garis BERARAH A->B (cross-product z).
    >0 = sisi NORMAL+ (rotasi +90° dari A->B, kami sebut 'maju'); <0 = seberang; 0 = persis di garis."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _segmen_potong(p, q, a, b):
    """Ruas gerak kaki p->q memotong RUAS garis a->b? (uji orientasi standar). Pakai
    ruas—bukan garis tak-hingga—agar orang yang lewat di luar bentang garis tak terhitung."""
    d1 = _cross_sign(p, q, a)
    d2 = _cross_sign(p, q, b)
    d3 = _cross_sign(a, b, p)
    d4 = _cross_sign(a, b, q)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


class Tripwire:
    """Deteksi penyeberangan garis berarah oleh TITIK-KAKI (BOTTOM_CENTER) per track.
    Simpan posisi kaki terakhir tiap track; saat ruas gerak kaki memotong ruas garis,
    lahirkan arah 'maju' (ke sisi normal+) / 'mundur' (sebaliknya). Murni-state -> mudah
    diuji. Cooldown per (track,arah) meredam getar bolak-balik di ambang garis. Titik
    dalam FRAKSI [0..1] (bebas-resolusi; dipetakan dari box saat runtime, seperti abaikan)."""

    def __init__(self, a, b, cooldown_s=8.0):
        self.a, self.b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
        self.cooldown = cooldown_s
        self.last = {}     # tid -> (kaki, t)  posisi kaki terakhir
        self.fired = {}    # (tid, arah) -> t terakhir lahir

    def update(self, tid, kaki, t):
        """Return 'maju'/'mundur' bila kaki `tid` BARU menyeberang garis, else None."""
        prev = self.last.get(tid)
        self.last[tid] = (kaki, t)
        if prev is None:
            return None
        if not _segmen_potong(prev[0], kaki, self.a, self.b):
            return None
        arah = "maju" if _cross_sign(self.a, self.b, kaki) > 0 else "mundur"
        key = (tid, arah)
        if t - self.fired.get(key, -1e18) < self.cooldown:
            return None
        self.fired[key] = t
        return arah

    def prune(self, t, ttl=30.0):
        """Lupakan track yg lama tak terlihat -> dict tak membengkak; ID baru mulai bersih."""
        for tid in [k for k, (_, tt) in list(self.last.items()) if t - tt > ttl]:
            del self.last[tid]


def _garis_lines(gp):
    """Normalisasi garis_periksa -> list of dict {garis,label,nama}. Terima BENTUK:
    dict tunggal (legacy 1 garis) ATAU list (banyak garis). Buang entri tak valid."""
    if isinstance(gp, dict):
        gp = [gp]
    out = []
    if isinstance(gp, list):
        for g in gp:
            if isinstance(g, dict) and isinstance(g.get("garis"), list) and len(g["garis"]) == 2:
                out.append(g)
    return out


def _garis_sig(gp):
    """Tanda-tangan ringkas garis_periksa utk deteksi perubahan (live-reload tripwire)."""
    return json.dumps(gp, sort_keys=True) if gp is not None else ""


class Worker(threading.Thread):
    """Satu thread deteksi per kamera garasi-ringan. Model DIBAGI antar-worker
    (satu load VRAM) -> predict di-serialize `mlock` (garasi fps rendah, kontensi
    minim). Jadwal/enabled dibaca live dari watcher (per-kamera by nama)."""

    def __init__(self, nama, watcher, model, mlock, writer, args):
        super().__init__(name=f"garasi:{nama}", daemon=True)
        self.nama, self.watcher, self.model = nama, watcher, model
        self.mlock, self.writer, self.args = mlock, writer, args
        self.stop = False

    def _cam(self):
        for k in self.watcher.cfg.get("kamera", []):
            if k.get("nama") == self.nama and k.get("peran") == "garasi-ringan":
                return k
        return None

    def _buat_tripwires(self, cam):
        """List of (Tripwire, labels, nama) dari garis_periksa (bisa banyak garis).
        labels memetakan sisi normal -> nama arah user (maju/mundur -> mis. naik/turun).
        nama = label garis (mis. 'gerbang') utk membedakan garis mana yg diseberangi."""
        out = []
        for i, g in enumerate(_garis_lines(cam.get("garis_periksa"))):
            lab = g.get("label") or {}
            labels = {"maju": lab.get("maju") or "maju", "mundur": lab.get("mundur") or "mundur"}
            nama = g.get("nama") or f"garis{i + 1}"
            out.append((Tripwire(g["garis"][0], g["garis"][1]), labels, nama))
        return out

    def _track_feet(self, frame, abaikan):
        """model.track -> (ada_person: bool, feet: [(tid,(fx,fy))]). Titik-kaki =
        BOTTOM_CENTER box dalam FRAKSI (pusat-x, tepi-bawah-y). Tolak box yg PUSATnya
        di zona abaikan (konsisten ada_person). Dipanggil DI BAWAH mlock — state BoT-SORT
        menempel pd model bersama; asumsi garasi = 1 kamera bertripwire (lihat main)."""
        result = self.model.track(frame, persist=True, tracker="botsort_reid.yaml",
                                  classes=[0], conf=self.args.conf, verbose=False)[0]
        boxes = result.boxes
        ada, feet = False, []
        if boxes is None or boxes.xyxy is None or len(boxes) == 0:
            return ada, feet
        h, w = result.orig_shape
        ids = boxes.id.tolist() if boxes.id is not None else [None] * len(boxes)
        for (x1, y1, x2, y2), tid in zip(boxes.xyxy.tolist(), ids):
            cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
            if _dalam_rect(cx, cy, abaikan):
                continue
            ada = True
            if tid is not None:
                feet.append((int(tid), ((x1 + x2) / 2 / w, y2 / h)))   # titik-kaki fraksi
        return ada, feet

    def run(self):
        cam = self._cam()
        if not cam:
            return
        try:
            source = FrameSource(stream_url(cam["stream"]))
        except Exception as e:
            print(f"[GARASI:{self.nama}] gagal buka stream ({e!r})", flush=True)
            return
        deb = Debounce(self.args.need_frames, self.args.cooldown)
        abaikan = [tuple(r) for r in cam.get("abaikan", [])]   # kotak fraksi: phantom/air
        gate = None if not self.args.motion else MotionGate(
            self.args.motion_area, self.args.motion_delta, abaikan=abaikan)
        trips = self._buat_tripwires(cam)                  # [] bila kamera tak punya garis_periksa
        trip_sig = _garis_sig(cam.get("garis_periksa"))    # tanda-tangan utk live-reload
        i, last_hb = 0, 0.0
        n_gerak = n_yolo = 0                                # statistik utk tuning (heartbeat)
        last_frac = 0.0
        print(f"[GARASI:{self.nama}] worker mulai stream={cam['stream']} "
              f"motion={'on' if gate else 'off'} abaikan={len(abaikan)} zona "
              f"tripwire={len(trips)} garis " + (",".join(n for _, _, n in trips) if trips else "off"), flush=True)
        try:
            for t, frame in source.frames():
                if self.stop:
                    break
                cam = self._cam()
                if cam is None or not cam.get("enabled", True):
                    break                                   # dihapus/dimatikan -> keluar (tak di-restart)
                sig = _garis_sig(cam.get("garis_periksa"))  # garis diedit dari viewer -> reload live
                if sig != trip_sig:
                    trips = self._buat_tripwires(cam)
                    trip_sig = sig
                    print(f"[GARASI:{self.nama}] garis_periksa berubah -> {len(trips)} garis "
                          + (",".join(n for _, _, n in trips) if trips else "(kosong)"), flush=True)
                i += 1
                notif = dalam_jendela(t, cam.get("jadwal", []))   # jadwal = gerbang NOTIF (deteksi jalan 24/7)
                if t - last_hb >= 30:
                    print(f"[HIDUP] {self.nama} frame#{i} notif={'aktif' if notif else 'senyap'} "
                          f"gerak={n_gerak} yolo={n_yolo} frac_terakhir={last_frac:.4f}", flush=True)
                    last_hb = t
                if i % self.args.every:
                    continue                                # sampling frame ke-N (deteksi TETAP 24/7 spt taman)
                if gate is not None:                        # gerbang MURAH dulu: ada gerak?
                    gerak, last_frac = gate.ada_gerak(frame)
                    if not gerak:
                        deb.on_frame(False, t)              # scene diam -> tak ada orang (reset streak)
                        continue                            # YOLO DILEWATI (hemat + redam phantom statis)
                    n_gerak += 1
                if trips:                                    # alur BoT-SORT: presence + tripwire berarah (banyak garis)
                    with self.mlock:                        # serialize akses model bersama (state tracker di model)
                        ada, feet = self._track_feet(frame, abaikan)
                    for trip, labels, nama in trips:        # tiap GARIS diuji thd tiap kaki
                        for tid, kaki in feet:
                            arah = trip.update(tid, kaki, t)
                            if arah:
                                label = labels[arah]
                                print(f"[GARASI:{self.nama}] LINTAS {nama}/{label} (track#{tid}) @ "
                                      f"{time.strftime('%H:%M:%S')}{' (dry-run)' if self.args.dry_run else ''}", flush=True)
                                if self.writer:
                                    self.writer.tulis(ts=t, kind="garasi", zone=nama, notify=notif,
                                                      payload={"kind": "garasi", "camera": self.nama, "at": t,
                                                               "arah": label, "lintas": True, "garis": nama})
                        trip.prune(t)
                else:                                        # kamera tanpa garis -> jalur predict murah lama
                    with self.mlock:
                        ada = ada_person(self.model, frame, self.args.conf, abaikan)
                n_yolo += 1
                if deb.on_frame(ada, t):
                    print(f"[GARASI:{self.nama}] orang terdeteksi @ {time.strftime('%H:%M:%S')}"
                          f"{' (dry-run)' if self.args.dry_run else ''}", flush=True)
                    if self.writer:
                        self.writer.tulis(ts=t, kind="garasi", notify=notif,
                                          payload={"kind": "garasi", "camera": self.nama, "at": t})
        finally:
            source.release()
            print(f"[GARASI:{self.nama}] worker berhenti", flush=True)


def kamera_garasi(cfg):
    """nama-nama kamera peran garasi-ringan yang enabled (punya nama unik)."""
    out = []
    for k in cfg.get("kamera", []):
        if (k.get("peran") == "garasi-ringan" and k.get("enabled", True)
                and k.get("nama") and k["nama"] not in out):
            out.append(k["nama"])
    return out


def main():
    ap = argparse.ArgumentParser(description="Detektor orang garasi (gated by jadwal)")
    ap.add_argument("--cameras-file", default="cameras.json")
    ap.add_argument("--model", default="yolo11s.pt")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--every", type=int, default=5, help="cek tiap ke-N frame (fps rendah)")
    ap.add_argument("--need-frames", type=int, default=3)
    ap.add_argument("--cooldown", type=float, default=60)
    ap.add_argument("--no-motion", dest="motion", action="store_false",
                    help="matikan gerbang gerak (YOLO tiap frame ke-N; boros + rawan phantom)")
    ap.add_argument("--motion-area", type=float, default=0.0025,
                    help="fraksi blob-gerak terbesar minimal utk memicu YOLO")
    ap.add_argument("--motion-delta", type=int, default=25,
                    help="ambang beda piksel (0-255) dianggap berubah")
    ap.set_defaults(motion=True)
    ap.add_argument("--dry-run", action="store_true", help="deteksi + log saja, JANGAN tulis DB (uji)")
    args = ap.parse_args()

    watcher = ConfigWatcher(args.cameras_file)

    from ultralytics import YOLO
    model = YOLO(args.model)                     # DIBAGI semua worker (satu load VRAM)
    mlock = threading.Lock()
    writer = None if args.dry_run else EventWriter()

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))

    print(f"[GARASI] supervisor mulai model={args.model} dry_run={args.dry_run}", flush=True)
    workers = {}                                 # nama -> Worker
    warned_trip = False
    try:
        while not stop["v"]:
            watcher.reload_if_changed()
            want = kamera_garasi(watcher.cfg)
            if not warned_trip:                  # BoT-SORT state menempel pd model bersama
                trips = [k["nama"] for k in watcher.cfg.get("kamera", [])
                         if k.get("nama") in want and _garis_lines(k.get("garis_periksa"))]
                if len(trips) > 1:
                    print(f"[GARASI] PERINGATAN: {len(trips)} kamera bertripwire berbagi state "
                          f"BoT-SORT satu model ({trips}) -> ID track bisa tercampur. "
                          f"Untuk >1, jalankan proses run_garasi terpisah per kamera.", flush=True)
                    warned_trip = True
            for nama in want:                    # start baru / restart yang mati (stream putus)
                w = workers.get(nama)
                if w is None or not w.is_alive():
                    nw = Worker(nama, watcher, model, mlock, writer, args)
                    workers[nama] = nw
                    nw.start()
            for nama, w in list(workers.items()):  # stop yang tak lagi diinginkan
                if nama not in want:
                    w.stop = True
                    del workers[nama]
                    print(f"[GARASI] lepas worker {nama} (dihapus/dimatikan config)", flush=True)
            time.sleep(3)                        # rekonsiliasi berkala + saat cameras.json berubah
    finally:
        for w in workers.values():
            w.stop = True
        time.sleep(0.5)
        if writer:
            writer.close()
        print("[GARASI] supervisor berhenti.", flush=True)


if __name__ == "__main__":
    main()
