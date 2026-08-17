#!/usr/bin/env python3
"""Detektor orang MULTI-KAMERA ringan (alur TERPISAH dari pipeline taman penuh).

SATU service, banyak stream: supervisor membaca cameras.json dan menjalankan satu
worker-thread per kamera peran `garasi-ringan`. Menambah kamera = tambah entri di
cameras.json (dari viewer/Telegram) -> supervisor start worker-nya LIVE, tanpa
systemctl. Model YOLO DIBAGI semua worker (satu load VRAM; predict di-serialize).

Tiap worker: baca stream go2rtc (nama, bukan URL berkredensial), deteksi kelas
`person` (yolo11s, tanpa tracker), dan HANYA di dalam jendela jadwal -> event
`garasi` (tag camera) ke cctv.db (notify=1) -> bot kirim notif. Di luar jendela:
worker hidup, lewati inferensi (hemat GPU). Jadwal/enabled dibaca LIVE (mtime).

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


def ada_person(model, frame, conf):
    for r in model.predict(frame, classes=[0], conf=conf, verbose=False):
        if r.boxes is not None and len(r.boxes) > 0:
            return True
    return False


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
        i, last_hb = 0, 0.0
        print(f"[GARASI:{self.nama}] worker mulai stream={cam['stream']}", flush=True)
        try:
            for t, frame in source.frames():
                if self.stop:
                    break
                cam = self._cam()
                if cam is None or not cam.get("enabled", True):
                    break                                   # dihapus/dimatikan -> keluar (tak di-restart)
                i += 1
                aktif = dalam_jendela(t, cam.get("jadwal", []))
                if t - last_hb >= 30:
                    print(f"[HIDUP] {self.nama} frame#{i} jendela={'aktif' if aktif else 'tidur'}", flush=True)
                    last_hb = t
                if not aktif or (i % self.args.every):
                    continue                                # luar jendela / bukan frame ke-N
                with self.mlock:                            # serialize akses model bersama
                    ada = ada_person(self.model, frame, self.args.conf)
                if deb.on_frame(ada, t):
                    print(f"[GARASI:{self.nama}] orang terdeteksi @ {time.strftime('%H:%M:%S')}"
                          f"{' (dry-run)' if self.args.dry_run else ''}", flush=True)
                    if self.writer:
                        self.writer.tulis(ts=t, kind="garasi", notify=1,
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
    ap.add_argument("--every", type=int, default=5, help="inferensi tiap ke-N frame (fps rendah)")
    ap.add_argument("--need-frames", type=int, default=3)
    ap.add_argument("--cooldown", type=float, default=60)
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
