# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "opencv-python>=4.8",
# ]
# ///
"""Tool bantu lesson 0004 — gambar zona poligon di atas satu frame video.

    uv run pipeline/draw_zone.py samples/siang_3m.mp4 kolam
    uv run pipeline/draw_zone.py samples/siang_3m.mp4 teras --frame 500
    uv run pipeline/draw_zone.py --list

Kontrol di jendela: klik kiri = tambah titik | u = undo | s/ENTER = simpan
(min. 3 titik) | q/ESC = batal. Koordinat disimpan dalam piksel resolusi asli
video ke zones.json (satu file untuk semua zona; nama sama = menimpa).
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

ZONES_FILE = Path(__file__).parent.parent / "zones.json"
MAX_DISPLAY_W = 1280


def load_zones() -> dict:
    if ZONES_FILE.exists():
        return json.loads(ZONES_FILE.read_text())
    return {"video_size": None, "zones": {}}


def main() -> None:
    p = argparse.ArgumentParser(description="Gambar zona poligon di atas frame video.")
    p.add_argument("video", nargs="?", help="path file video")
    p.add_argument("name", nargs="?", help="nama zona, mis. kolam / teras / jalan")
    p.add_argument("--frame", type=int, default=0, help="nomor frame yang dipakai")
    p.add_argument("--list", action="store_true", help="tampilkan zona tersimpan lalu keluar")
    args = p.parse_args()

    if args.list:
        data = load_zones()
        if not data["zones"]:
            print("belum ada zona tersimpan.")
        for name, pts in data["zones"].items():
            print(f"  {name:<12} {len(pts)} titik  {pts}")
        return

    if not args.video or not args.name:
        raise SystemExit("butuh: video + nama zona (atau --list). lihat --help")

    cap = cv2.VideoCapture(args.video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, args.frame)
    ok, img = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"gagal membaca frame {args.frame} dari {args.video}")

    h, w = img.shape[:2]
    scale = min(1.0, MAX_DISPLAY_W / w)
    disp_size = (int(w * scale), int(h * scale))

    data = load_zones()
    points: list[list[int]] = list(data["zones"].get(args.name, []))

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append([int(x / scale), int(y / scale)])

    win = f"zona: {args.name}  (klik=titik, u=undo, s=simpan, q=batal)"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(win, on_mouse)

    while True:
        canvas = cv2.resize(img, disp_size)
        # zona lain: abu-abu tipis, sebagai konteks
        for other, pts in data["zones"].items():
            if other == args.name or len(pts) < 3:
                continue
            disp = np.array([[int(px * scale), int(py * scale)] for px, py in pts],
                            dtype=np.int32)
            cv2.polylines(canvas, [disp], True, (160, 160, 160), 1)
        # zona yang sedang digambar: merah
        disp_pts = [[int(px * scale), int(py * scale)] for px, py in points]
        for i, (dx, dy) in enumerate(disp_pts):
            cv2.circle(canvas, (dx, dy), 4, (0, 0, 255), -1)
            if i > 0:
                cv2.line(canvas, tuple(disp_pts[i - 1]), (dx, dy), (0, 0, 255), 2)
        if len(disp_pts) >= 3:
            cv2.line(canvas, tuple(disp_pts[-1]), tuple(disp_pts[0]), (0, 0, 255), 1)
        cv2.putText(canvas, f"{args.name}: {len(points)} titik", (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 255), 2)
        cv2.imshow(win, canvas)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), 27):
            print("batal — tidak disimpan.")
            break
        if key == ord("u") and points:
            points.pop()
        if key in (ord("s"), 13):
            if len(points) < 3:
                print("minimal 3 titik.")
                continue
            data["video_size"] = [w, h]
            data["zones"][args.name] = points
            ZONES_FILE.write_text(json.dumps(data, indent=2))
            print(f"tersimpan: zona '{args.name}' ({len(points)} titik) → {ZONES_FILE}")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
