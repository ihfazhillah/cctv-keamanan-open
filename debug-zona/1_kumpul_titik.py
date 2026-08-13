#!/usr/bin/env python3
"""LANGKAH 1 — Kumpulkan titik-kaki + peta MUNCUL/HILANG dari banyak klip.

Ide: zona tidak digambar dengan mata, tapi diturunkan dari perilaku yang direkam.
Setiap orang = kotak deteksi; jangkarnya = tengah-bawah (kaki) = petak lantai tempat
ia berdiri. Titik LAHIR (muncul) & MATI (hilang) tiap track menandai BATAS ke wilayah
tak-teramati (jalanan / interior rumah). Menaburkan titik dari banyak video -> peta
kepadatan -> tempat nyata (pintu, tepi) muncul sebagai gumpalan.

Pakai:
    .venv/bin/python debug-zona/1_kumpul_titik.py out/live/klip_*rumah*.mp4
Keluaran (ke debug-zona/out/):
    points.json    : semua titik birth+death [[x,y],...] (ruang 640x360)
    birthdeath.jpg : sebaran titik muncul(hijau)/hilang(oranye) di atas poligon lama
"""
import sys, json
from pathlib import Path
import numpy as np, cv2, supervision as sv
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent.parent
MODEL, CONF, W, H = str(ROOT / "yolo26l.pt"), 0.15, 640, 360   # sama spt produksi
TRACKER = str(ROOT / "botsort_reid.yaml")
OUT = Path(__file__).resolve().parent / "out"; OUT.mkdir(exist_ok=True)

zdata = json.loads((ROOT / "zones-102.json").read_text())["zones"]
polys = {n: np.array(p, np.int32) for n, p in zdata.items()}

def foot(xyxy):                       # jangkar BOTTOM_CENTER (tengah-bawah = kaki)
    x1, y1, x2, y2 = xyxy
    return int((x1 + x2) / 2), int(y2)

births, deaths, sample = [], [], None
for clip in sys.argv[1:]:
    model = YOLO(MODEL)               # instance baru = reset tracker antar-klip
    cap = cv2.VideoCapture(clip)
    first, last = {}, {}
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if fr.shape[1] != W or fr.shape[0] != H:
            fr = cv2.resize(fr, (W, H))
        if sample is None:
            sample = fr.copy()
        res = model.track(fr, persist=True, tracker=TRACKER, verbose=False,
                          conf=CONF, classes=[0])[0]      # classes=[0] -> person
        d = sv.Detections.from_ultralytics(res)
        if d.tracker_id is None:
            continue
        for i in range(len(d)):
            tid = int(d.tracker_id[i]); fx, fy = foot(d.xyxy[i])
            first.setdefault(tid, (fx, fy))               # posisi LAHIR (sekali)
            last[tid] = (fx, fy)                           # posisi terakhir (MATI)
    births += list(first.values()); deaths += list(last.values())
    cap.release()
    print(f"  {Path(clip).stem}: {len(first)} track")

pts = births + deaths
(OUT / "points.json").write_text(json.dumps(pts))
print(f"\nTotal titik birth+death: {len(pts)} -> {OUT/'points.json'}")

vis = (sample if sample is not None else np.full((H, W, 3), 40, np.uint8)).copy()
for n, p in polys.items():
    cv2.polylines(vis, [p], True, (90, 90, 90), 1)
cv2.polylines(vis, [polys["pintu"]], True, (0, 0, 255), 2)     # merah = pintu (lama)
for x, y in births: cv2.circle(vis, (x, y), 3, (0, 220, 0), -1)   # hijau = muncul
for x, y in deaths: cv2.circle(vis, (x, y), 3, (0, 140, 255), -1) # oranye = hilang
cv2.imwrite(str(OUT / "birthdeath.jpg"), vis)
print(f"Sebaran -> {OUT/'birthdeath.jpg'}  (hijau=muncul, oranye=hilang)")
