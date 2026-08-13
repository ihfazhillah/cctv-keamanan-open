#!/usr/bin/env python3
"""LANGKAH 3 — Uji end-to-end: jalankan pipeline NYATA (deteksi -> zona -> RuleEngine)
pada klip, cetak event masuk/keluar rumah. Untuk validasi poligon+aturan sebelum deploy.

Pakai:
    .venv/bin/python debug-zona/3_uji_e2e.py out/live/klip_keluar_rumah_*.mp4
Env opsional: MINPRES=0.5 (ambang umur track), ZONEF=zones-102.json.

CATATAN: butuh TG_TOKEN/TG_CHAT_ID hanya agar run_live bisa di-import; skrip ini tidak
mengirim apa pun. Set dummy: TG_TOKEN=x TG_CHAT_ID=x .venv/bin/python ...
"""
import os, sys, cv2
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "pipeline"))
os.environ.setdefault("TG_TOKEN", "x"); os.environ.setdefault("TG_CHAT_ID", "x")
import supervision as sv
from live import RuleEngine
from run_live import muat_zone, Occupancy, DetectorTracker
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZONEF = os.environ.get("ZONEF", str(ROOT / "zones-102.json"))
MINPRES = float(os.environ.get("MINPRES", "1.0"))
zones = muat_zone(ZONEF); occ = Occupancy(zones)

def house_events(clip):
    eng = RuleEngine(exit_hysteresis=3.0, enter_inertia=1.0, min_presence_s=MINPRES,
                     ambang_s=3.0, loiter_s=30)
    det = DetectorTracker(str(ROOT / "yolo26l.pt"), conf=0.15, classes=(0,))
    cap = cv2.VideoCapture(clip); t, evs = 0.0, []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if fr.shape[1] != 640:
            fr = cv2.resize(fr, (640, 360))
        occupied, tz = occ.of(det.detect(fr))
        evs += eng.update(occupied, {int(k): v for k, v in tz.items()}, t)
        t += 0.05
    evs += eng.flush()
    return [e for e in evs if "rumah" in e["kind"]]

for clip in sys.argv[1:]:
    ev = house_events(clip)
    print(f"\n{Path(clip).stem}")
    for e in ev:
        print(f"    {e['kind']}  @{round(e['at'], 2)}")
    if not ev:
        print("    (tak ada event rumah)")
