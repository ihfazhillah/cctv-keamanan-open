"""Test gerbang-gerak + zona-abaikan garasi (butuh cv2/numpy; terpisah dari
test_run_garasi yg nol-deps). Membuktikan: scene diam -> tak ada gerak (YOLO
dilewati), blob besar -> gerak, zona-abaikan menolak phantom (baik di diff gerak
maupun di penerimaan box YOLO).

    uv run pipeline/test_garasi_motion.py
"""
import numpy as np
import cv2
from run_garasi import MotionGate, ada_person, _dalam_rect


def _bgr(gray):
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def check(name, cond):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
    return bool(cond)


# --- fake YOLO utk uji ada_person tanpa model beneran ---
class _Box:
    def __init__(self, xyxy):
        self.xyxy = [np.array(xyxy, dtype=float)]


class _Res:
    def __init__(self, boxes, shape=(360, 640)):
        self.boxes = boxes
        self.orig_shape = shape


class _FakeModel:
    def __init__(self, boxes):
        self._boxes = boxes

    def predict(self, frame, classes=None, conf=None, verbose=False):
        return [_Res(self._boxes)]


def main():
    r = []

    # 1) _dalam_rect
    rects = [(0.0, 0.0, 0.20, 0.30)]
    r.append(check("dalam_rect: pusat di pojok -> True", _dalam_rect(0.07, 0.13, rects)))
    r.append(check("dalam_rect: pusat di tengah -> False", not _dalam_rect(0.5, 0.5, rects)))

    # 2) MotionGate: dua frame IDENTIK -> tak ada gerak
    g = MotionGate(min_area_frac=0.0025, delta_thresh=25)
    base = np.full((360, 640), 40, np.uint8)
    g.ada_gerak(_bgr(base))                       # baseline (warmup -> False)
    gerak, frac = g.ada_gerak(_bgr(base))
    r.append(check("frame identik -> diam", (not gerak) and frac == 0.0))

    # 3) blob besar (orang) -> gerak
    f = base.copy(); cv2.rectangle(f, (300, 150), (340, 260), 200, -1)
    gerak, frac = g.ada_gerak(_bgr(f))
    r.append(check("blob orang -> gerak", gerak and frac > 0.0025))

    # 4) grain tersebar (noise IR) -> BUKAN gerak (blob terbesar kecil)
    g2 = MotionGate(min_area_frac=0.0025, delta_thresh=25)
    g2.ada_gerak(_bgr(base))
    noisy = base.copy()
    rng = np.arange(base.size).reshape(base.shape)
    noisy[(rng % 37) == 0] = 200                  # titik-titik tersebar (bukan blob)
    gerak, frac = g2.ada_gerak(_bgr(noisy))
    r.append(check("grain tersebar -> bukan gerak", not gerak))

    # 5) blob DI DALAM zona-abaikan -> di-nol-kan -> bukan gerak (mis. riak air)
    g3 = MotionGate(min_area_frac=0.0025, delta_thresh=25, abaikan=[(0.0, 0.0, 0.20, 0.30)])
    g3.ada_gerak(_bgr(base))
    fa = base.copy(); cv2.rectangle(fa, (10, 10), (90, 90), 200, -1)   # blob di pojok kiri-atas
    gerak, frac = g3.ada_gerak(_bgr(fa))
    r.append(check("blob di zona-abaikan -> bukan gerak", not gerak))

    # 6) ada_person: box PUSAT di zona-abaikan -> ditolak (phantom)
    mabaikan = _FakeModel([_Box([28, 21, 61, 73])])   # pusat ~ (0.07,0.13)
    r.append(check("box phantom di abaikan -> ada_person False",
                   not ada_person(mabaikan, np.zeros((360, 640, 3), np.uint8),
                                  0.35, [(0.0, 0.0, 0.20, 0.30)])))
    r.append(check("box phantom TANPA abaikan -> ada_person True",
                   ada_person(mabaikan, np.zeros((360, 640, 3), np.uint8), 0.35)))

    # 7) ada_person: box orang di tengah -> diterima walau ada zona-abaikan
    morang = _FakeModel([_Box([300, 150, 360, 300])])  # pusat ~ (0.52,0.62)
    r.append(check("box orang tengah -> ada_person True",
                   ada_person(morang, np.zeros((360, 640, 3), np.uint8),
                              0.35, [(0.0, 0.0, 0.20, 0.30)])))

    print()
    if all(r):
        print(f"ALL PASS ({len(r)}/{len(r)})")
    else:
        print(f"FAIL {r.count(False)}/{len(r)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
