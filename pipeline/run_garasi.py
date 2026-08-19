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
from rules import (_cross_sign, _segmen_potong, Tripwire, FootTracker,  # noqa: F401
                   _garis_lines, _garis_sig)

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


# ══ Rule murni -> rules.py (portable/Colab; di-import di atas) ═══════════════════════


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

    def _predict_feet(self, frame, abaikan):
        """model.predict (BUKAN track) -> (ada_person: bool, feet: [(fx,fy)]). Titik-kaki
        = BOTTOM_CENTER box FRAKSI. Deteksi di conf RENDAH `cross_conf` (tangkap pelari
        jauh/buram yg jatuh <0.35 saat menyeberang); `ada` (presence) tetap butuh conf
        >= args.conf. FootTracker (per-worker) yg beri ID — bukan BoT-SORT yg flicker
        utk objek cepat. Tolak box yg PUSATnya di zona abaikan."""
        result = self.model.predict(frame, classes=[0], conf=self.args.cross_conf,
                                    verbose=False)[0]
        boxes = result.boxes
        ada, feet = False, []
        if boxes is None or boxes.xyxy is None or len(boxes) == 0:
            return ada, feet
        h, w = result.orig_shape
        confs = boxes.conf.tolist()
        for (x1, y1, x2, y2), cf in zip(boxes.xyxy.tolist(), confs):
            cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
            if _dalam_rect(cx, cy, abaikan):
                continue
            if cf >= self.args.conf:                 # presence butuh conf penuh (redam FP)
                ada = True
            feet.append(((x1 + x2) / 2 / w, y2 / h))  # crossing pakai conf rendah -> semua box
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
        ft = FootTracker()                                 # ID titik-kaki ringan (ganti BoT-SORT)
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
                ev = 1 if trips else self.args.every        # tripwire = frame RAPAT (10fps) utk pelari cepat;
                if i % ev:                                   # tanpa garis = sampling jarang (hemat)
                    continue                                # deteksi TETAP 24/7 spt taman
                if gate is not None:                        # gerbang MURAH dulu: ada gerak?
                    gerak, last_frac = gate.ada_gerak(frame)
                    if not gerak:
                        deb.on_frame(False, t)              # scene diam -> tak ada orang (reset streak)
                        continue                            # YOLO DILEWATI (hemat + redam phantom statis)
                    n_gerak += 1
                if trips:                                    # predict conf-rendah + FootTracker + tripwire berarah
                    with self.mlock:                        # serialize akses model bersama
                        ada, feet = self._predict_feet(frame, abaikan)
                    for tid, kaki in ft.update(feet, t):    # ID stabil dari FootTracker (bukan BoT-SORT)
                        for trip, labels, nama in trips:    # tiap GARIS diuji thd kaki ber-ID
                            arah = trip.update(tid, kaki, t)
                            if arah:
                                label = labels[arah]
                                print(f"[GARASI:{self.nama}] LINTAS {nama}/{label} (track#{tid}) @ "
                                      f"{time.strftime('%H:%M:%S')}{' (dry-run)' if self.args.dry_run else ''}", flush=True)
                                if self.writer:
                                    self.writer.tulis(ts=t, kind="garasi", zone=nama, notify=notif,
                                                      payload={"kind": "garasi", "camera": self.nama, "at": t,
                                                               "arah": label, "lintas": True, "garis": nama})
                    for trip, _, _ in trips:
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
    ap.add_argument("--conf", type=float, default=0.35, help="ambang presence 'ada orang'")
    ap.add_argument("--cross-conf", type=float, default=0.20,
                    help="ambang deteksi utk tripwire (lebih rendah: tangkap pelari jauh/buram)")
    ap.add_argument("--every", type=int, default=5, help="cek tiap ke-N frame (kamera TANPA garis; bergaris=1)")
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
    try:
        while not stop["v"]:
            watcher.reload_if_changed()
            want = kamera_garasi(watcher.cfg)
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
