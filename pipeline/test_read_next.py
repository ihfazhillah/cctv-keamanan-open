"""Test read_next — RONDE RTSP: single cap.read() (drop-stale kini DI DALAM OpenCV).

    uv run pipeline/test_read_next.py   (atau: python3 ...)

Nol-deps.

PERUBAHAN dari versi lama: read_next tak lagi drain-loop `while grab()` + `retrieve()`.
Di RTSP, `grab()` tak pernah balas False (stream tak habis) -> loop menggantung. Drop-stale
sekarang ditangani OpenCV lewat CAP_PROP_BUFFERSIZE=1, jadi read_next cukup satu `cap.read()`.

    def read_next(cap) -> (t, frame) | None
        ok, frame = cap.read()
        if not ok: return None
        return time.time(), frame

Test lama (FakeCap grab/retrieve, drain-loop) DIPENSIUNKAN -- ia mengunci logika yang kini
BUKAN milik kita (= fake-ramah-sembunyikan-api penuh siklus). Yang masih milik kita & diuji
di sini: unpack (ok, frame) benar (bug potongan-6 "frame = cap.retrieve()" lupa unpack tuple),
ok=False -> None, stempel t = time.time() (wall-clock). Bukti drop-stale RTSP = MENJALANKAN
di kamera nyata; fake tak bisa membuktikan cv2 asli (itu batas jujurnya).
"""

import time
from live import read_next


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


class FakeCap:
    """read() mengembalikan (ok, frame) — bentuk API cv2 yang SEBENARNYA."""
    def __init__(self, hasil):
        self.hasil = list(hasil)   # daftar (ok, frame)
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.hasil.pop(0)


def main():
    results = []

    # R1) read sukses -> (t, frame); t = wall-clock (before <= t <= after); tepat satu read.
    before = time.time()
    cap = FakeCap([(True, "frameA")])
    out = read_next(cap)
    after = time.time()
    t, frame = out
    results.append(check("R1 frame diteruskan", frame, "frameA"))
    results.append(check("R1 t wall-clock (before<=t<=after)", before <= t <= after, True))
    results.append(check("R1 tepat satu cap.read()", cap.reads, 1))

    # R2) read gagal (ok=False) -> None (stream putus, bukan crash).
    cap = FakeCap([(False, None)])
    results.append(check("R2 read gagal -> None", read_next(cap), None))

    # R3) UNPACK benar: frame = frame, BUKAN tuple (True,'X') utuh (bug potongan-6).
    cap = FakeCap([(True, "X")])
    _, frame = read_next(cap)
    results.append(check("R3 frame = 'X' (bukan tuple utuh)", frame, "X"))

    # R4) beruntun: tiap panggil = satu read, urut.
    cap = FakeCap([(True, "a"), (True, "b")])
    _, f1 = read_next(cap)
    _, f2 = read_next(cap)
    results.append(check("R4 beruntun a lalu b", (f1, f2), ("a", "b")))
    results.append(check("R4 dua panggil = dua read", cap.reads, 2))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
