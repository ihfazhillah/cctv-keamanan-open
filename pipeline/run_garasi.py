#!/usr/bin/env python3
"""Detektor orang GARASI — ringan & bergerbang-jadwal (alur TERPISAH dari taman).

Baca stream go2rtc (nama, bukan URL berkredensial), deteksi kelas `person`
(yolo11s, TANPA tracker/ReID), dan HANYA di dalam jendela jadwal -> lahirkan event
`garasi` ke cctv.db (notify=1) sehingga service bot mengirim notif teks. Di luar
jendela: proses TETAP hidup, lewati inferensi (hemat GPU). Jadwal dibaca LIVE dari
cameras.json (edit dari viewer -> berlaku tanpa restart, via mtime).

    uv run --env-file .env pipeline/run_garasi.py --cameras-file cameras.json

Slice pertama: deteksi + notif. Config UI viewer & tampil-di-viewer menyusul.
Logika murni (dalam_jendela, Debounce) teruji -> test_run_garasi.py.
"""
import os
import json
import time
import signal
import argparse
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
    cam = pilih_kamera(watcher.cfg)
    if not cam:
        print("[GARASI] tak ada kamera peran 'garasi-ringan' enabled -> keluar", flush=True)
        return
    print(f"[GARASI] mulai kamera={cam['nama']} stream={cam['stream']} "
          f"model={args.model} dry_run={args.dry_run}", flush=True)

    from ultralytics import YOLO
    model = YOLO(args.model)
    writer = None if args.dry_run else EventWriter()
    deb = Debounce(args.need_frames, args.cooldown)

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))

    source = FrameSource(stream_url(cam["stream"]))
    i = 0
    last_hb = 0.0
    try:
        for t, frame in source.frames():
            if stop["v"]:
                break
            watcher.reload_if_changed()
            cam = pilih_kamera(watcher.cfg) or cam          # enabled/jadwal live
            i += 1
            aktif = dalam_jendela(t, cam.get("jadwal", []))
            if t - last_hb >= 30:
                print(f"[HIDUP] {time.strftime('%H:%M:%S')} frame#{i} "
                      f"jendela={'aktif' if aktif else 'tidur'}", flush=True)
                last_hb = t
            if not aktif:
                continue                                    # luar jendela -> skip inferensi (hemat GPU)
            if i % args.every:
                continue                                    # fps rendah
            if deb.on_frame(ada_person(model, frame, args.conf), t):
                print(f"[GARASI] orang terdeteksi @ {time.strftime('%H:%M:%S')}"
                      f"{' (dry-run)' if args.dry_run else ' -> event'}", flush=True)
                if writer:
                    writer.tulis(ts=t, kind="garasi", notify=1,
                                 payload={"kind": "garasi", "camera": cam["nama"], "at": t})
    finally:
        source.release()
        if writer:
            writer.close()
        print("[GARASI] berhenti.", flush=True)


if __name__ == "__main__":
    main()
