"""Test FrameBuffer — jalankan cepat di PC praktik untuk iterasi.

    uv run pipeline/test_frame_buffer.py      (atau: python3 pipeline/test_frame_buffer.py)

Tanpa dependency (stdlib saja) → jalan di mesin mana pun, tak butuh GPU/RTSP.

KONTRAK yang diuji (age-based — buang by UMUR, karena klip diukur dalam DETIK):
    FrameBuffer(keep_s)          -> simpan hanya keep_s DETIK terakhir (relatif t terbaru)
    .add(t, frame)               -> append (t, frame) LALU evict yang lebih tua dari (t_terbaru - keep_s)
    .get(t0, t1) -> list[frame]  -> semua frame dengan t0 <= t <= t1 (INKLUSIF), urut waktu

Catatan: eviction relatif ke timestamp TERBARU yang masuk (stream: frame terbaru = "now").
Jendela yang dijaga = [t_terbaru - keep_s, t_terbaru], span persis keep_s detik.

Cara test: fake-frame = string ("f0","f1",...) supaya hasil mudah dibaca & di-assert.
Tiap check mandiri (buffer baru) biar kegagalan satu tak menular ke yang lain.
"""

from live import FrameBuffer


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def main():
    results = []

    # 1) EVICTION BY UMUR — keep_s=3, terbaru t=10 → simpan t >= 10-3=7 → f7..f10.
    #    (bukan "3 frame terakhir" — melainkan "3 detik terakhir")
    buf = FrameBuffer(keep_s=3)
    for t in range(11):
        buf.add(t, f"f{t}")
    results.append(check("age-eviction: 3 detik terakhir", buf.get(0, 100),
                         ["f7", "f8", "f9", "f10"]))

    # 1b) Batas: t == t_terbaru - keep_s IKUT tersimpan (f7 ada, f6 tidak).
    results.append(check("age-eviction: batas f7 masuk, f6 keluar", buf.get(6, 7), ["f7"]))

    # 1c) Yang tua benar-benar hilang.
    results.append(check("age-eviction: t lama tak terjangkau", buf.get(0, 6), []))

    # 2) TAK ADA COUNT-CAP dalam jendela — keep_s besar → SEMUA frame bertahan,
    #    berapa pun jumlahnya (membuktikan bukan deque(maxlen) lagi).
    buf = FrameBuffer(keep_s=10_000)
    for t in range(200):
        buf.add(t, f"f{t}")
    results.append(check("no count-cap: 200 frame bertahan", len(buf.get(0, 10_000)), 200))

    # 3) WINDOW inklusif di KEDUA ujung (keep_s besar → tak ada eviction mengganggu).
    buf = FrameBuffer(keep_s=10_000)
    for t in range(10):
        buf.add(t, f"f{t}")
    results.append(check("window [3,5] inklusif", buf.get(3, 5), ["f3", "f4", "f5"]))

    # 4) KLIP PRE/POST di sekitar trigger t=5 (pre=2, post=3) → [3..8].
    results.append(check("klip pre/post sekitar t=5", buf.get(5 - 2, 5 + 3),
                         ["f3", "f4", "f5", "f6", "f7", "f8"]))

    # 5) Buffer KOSONG → get mengembalikan [] (bukan error).
    results.append(check("buffer kosong", FrameBuffer(keep_s=10).get(0, 100), []))

    # 6) Window TANPA kecocokan → [].
    results.append(check("window di luar rentang", buf.get(50, 60), []))

    # 7) URUTAN WAKTU terjaga.
    results.append(check("urutan terjaga", buf.get(0, 100),
                         ["f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9"]))

    # 8) Titik tunggal t0==t1 mengambil tepat satu frame.
    results.append(check("titik tunggal t0==t1", buf.get(2, 2), ["f2"]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
