#!/usr/bin/env python3
"""LANGKAH 2 — Ubah taburan titik menjadi POLIGON zona (peta kepadatan -> kontur).

Titik mentah berisik. Untuk menemukan "tempat" stabil: hitung titik per piksel ->
kaburkan (Gaussian blur) -> ambil wilayah terpanas -> kontur -> sederhanakan jadi
poligon. Jebakan: bisa ada >1 titik panas (mis. pintu kiri vs jalur taman). Skrip ini
mengisolasi titik panas TERPILIH lewat kotak minat (ROI) supaya zona tak kegemukan.

Pakai:
    .venv/bin/python debug-zona/2_gambar_poligon.py            # ROI default = pintu kiri
    .venv/bin/python debug-zona/2_gambar_poligon.py X1 Y1 X2 Y2  # ROI custom (640x360)
Keluaran: debug-zona/out/poligon.jpg + cetak koordinat poligon (ruang 640x360).
"""
import sys, json
from pathlib import Path
import numpy as np, cv2

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
W, H = 640, 360
# ROI (kotak minat) untuk mengurung satu titik panas; default = area pintu kiri-bawah
ROI = tuple(map(int, sys.argv[1:5])) if len(sys.argv) >= 5 else (0, 120, 150, 240)

pts = np.array(json.loads((OUT / "points.json").read_text()))
zdata = json.loads((ROOT / "zones-102.json").read_text())["zones"]
polys = {n: np.array(p, np.int32) for n, p in zdata.items()}

# 1) peta kepadatan
dens = np.zeros((H, W), np.float32)
for x, y in pts:
    if 0 <= x < W and 0 <= y < H:
        dens[int(y), int(x)] += 1
dens = cv2.GaussianBlur(dens, (0, 0), sigmaX=8)

# 2) ambil 20% terpanas, batasi ke ROI (buang titik panas lain)
mask = (dens >= np.percentile(dens[dens > 0], 80)).astype(np.uint8) * 255
x1, y1, x2, y2 = ROI
roi = np.zeros_like(mask); roi[y1:y2, x1:x2] = 255
mask = cv2.bitwise_and(mask, roi)
mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

# 3) kontur komponen terbesar -> sederhanakan jadi poligon
cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
door = max(cnts, key=cv2.contourArea)
door = cv2.dilate(cv2.drawContours(np.zeros_like(mask), [door], -1, 255, -1),
                  np.ones((9, 9), np.uint8))              # longgar utk toleransi kaki
c2, _ = cv2.findContours(door, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
poly = cv2.approxPolyDP(max(c2, key=cv2.contourArea),
                        0.02 * cv2.arcLength(max(c2, key=cv2.contourArea), True), True).reshape(-1, 2)

print("POLIGON (ruang 640x360, salin ke zones-102.json):")
print(json.dumps(poly.tolist()))
# validasi: cakupan titik poligon lama vs baru
from matplotlib.path import Path as MPath
old_in = sum(1 for p in pts if MPath(polys["pintu"]).contains_point(p))
new_in = sum(1 for p in pts if MPath(poly).contains_point(p))
print(f"Cakupan titik: LAMA={old_in}  BARU={new_in}  (dari {len(pts)})")

# gambar hasil
heat = cv2.applyColorMap((dens / dens.max() * 255).astype(np.uint8), cv2.COLORMAP_JET)
base = cv2.imread(str(OUT / "birthdeath.jpg"))
vis = cv2.addWeighted(base if base is not None else heat, 0.6, heat, 0.4, 0)
cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 255, 0), 1)             # ROI
cv2.polylines(vis, [polys["pintu"]], True, (255, 0, 255), 1)        # magenta = lama
cv2.polylines(vis, [poly.astype(np.int32)], True, (255, 255, 255), 3)  # putih = baru
cv2.imwrite(str(OUT / "poligon.jpg"), vis)
print(f"Gambar -> {OUT/'poligon.jpg'}  (magenta=lama, putih=baru, kuning=ROI)")
