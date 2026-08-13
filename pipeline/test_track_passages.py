"""Test TrackPassageTracker — arah masuk/keluar PER TRACK (obat batas zone-centric).

    uv run pipeline/test_track_passages.py   (atau: python3 ...)

Nol-deps. Pelengkap test_passages.py (zone-centric, dipertahankan sbg referensi).
Beda kontrak: input = {track_id: nama_zona | None} per frame, BUKAN set gabungan.

KONTRAK:
    class TrackPassageTracker:
        def __init__(self, ambang_s=3.0, min_presence_s=0.0, ignore=frozenset({"jalan-utama"}))
        def update(self, track_zones, t) -> list[passage]   # track_zones: {tid: zona|None}
        def flush(self) -> list[passage]

Sumbu DEPTH: 0 luar | 1 jalan-masuk,tangga | 2 taman,dekat-kolam,teras | 3 pintu.
  - Batas PROPERTI (0<->1): dari lahir/mati track di tepi (depth 1).
  - Batas RUMAH (2<->3): hanya dari perpindahan TERAMATI (halaman/teras<->pintu).
"""

from live import TrackPassageTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames, ambang_s=3.0, min_presence_s=0.0):
    tr = TrackPassageTracker(ambang_s=ambang_s, min_presence_s=min_presence_s)
    out = []
    for t, tz in frames:
        out.extend(tr.update(tz, t))
    out.extend(tr.flush())
    return out


def main():
    results = []

    # T1) MASUK penuh dari jalanan ke pintu lalu LENYAP di pintu (masuk interior):
    #     MASUK property (lahir di tepi) lalu MASUK rumah (mati di pintu @ terakhir terlihat).
    out = run([(0.0, {1: "jalan-masuk"}), (1.0, {1: "taman"}), (2.0, {1: "teras"}),
               (3.0, {1: "pintu"}), (4.0, {1: "pintu"}), (9.0, {})])
    results.append(check("T1 jalanan->pintu->lenyap: MASUK property lalu MASUK rumah", out, [
        {"kind": "MASUK property", "at": 0.0},
        {"kind": "MASUK rumah", "at": 4.0},   # @ seen_t terakhir di pintu
    ]))

    # T2) KELUAR penuh: LAHIR di pintu (keluar dari interior) -> KELUAR rumah; lalu turun
    #     ke halaman & tepi lalu hilang di tepi -> KELUAR property.
    out = run([(0.0, {1: "pintu"}), (1.0, {1: "teras"}), (2.0, {1: "jalan-masuk"}),
               (3.0, {}), (7.0, {})])
    results.append(check("T2 lahir-pintu->jalanan: KELUAR rumah lalu KELUAR property", out, [
        {"kind": "KELUAR rumah", "at": 0.0},   # @ saat lahir di pintu
        {"kind": "KELUAR property", "at": 2.0},
    ]))

    # T3) LAHIR & MATI DI PINTU tanpa pernah ke halaman -> arah AMBIGU (tak tahu dari
    #     interior atau menuju interior) -> TAK menebak (jaga presisi). Butuh jejak
    #     halaman (pre/post) untuk memutuskan arah.
    out = run([(0.0, {1: "pintu"}), (1.0, {1: "pintu"}), (2.0, {}), (7.0, {})])
    results.append(check("T3 lahir&mati di pintu tanpa halaman -> ambigu (kosong)", out, []))

    # T4) MENJEJAK PINTU LALU BALIK: taman->pintu->taman. Lahir di taman (bukan pintu),
    #     mati di taman (bukan pintu) -> BUKAN masuk/keluar rumah. Inilah obat kedip batas:
    #     kaki yang cuma menyentuh ambang lalu balik ke halaman TIDAK memicu event.
    out = run([(0.0, {1: "taman"}), (1.0, {1: "pintu"}), (2.0, {1: "taman"}),
               (3.0, {1: "taman"}), (9.0, {})])
    results.append(check("T4 taman->pintu->taman: BUKAN event rumah (anti-kedip)", out, []))

    # T5) DUA ORANG independen di frame sama tak saling mengacau (fix multi-person).
    #     tid 1: jalanan->pintu (MASUK property+rumah). tid 2: pintu->taman (KELUAR rumah).
    out = run([(0.0, {1: "jalan-masuk", 2: "pintu"}),
               (1.0, {1: "taman", 2: "taman"}),
               (2.0, {1: "pintu", 2: "jalan-masuk"}),
               (3.0, {}), (7.0, {})])
    results.append(check("T5 dua track independen (urut temporal emisi)", out, [
        {"kind": "MASUK property", "at": 0.0},   # tid1 lahir di tepi (t=0)
        # sisanya keluar saat FINALISASI (urut per-track: tid1 dulu, lalu tid2):
        {"kind": "MASUK rumah", "at": 2.0},      # tid1 halaman->pintu lalu lenyap (@seen_t=2.0)
        {"kind": "KELUAR rumah", "at": 0.0},     # tid2 lahir di pintu lalu ke halaman (@start_t=0)
        {"kind": "KELUAR property", "at": 2.0},  # tid2 hilang dari tepi (@seen_t=2.0)
    ]))

    # T6) IGNORE jalan-utama = luar (depth 0): muncul di jalan-utama tak melahirkan track.
    out = run([(0.0, {1: "jalan-utama"}), (1.0, {1: "jalan-utama"}), (2.0, {})])
    results.append(check("T6 jalan-utama = luar -> tak ada passage", out, []))

    # T7) GRACE: kedip None sesaat (< ambang) di antara zona nyata TIDAK menutup track.
    #     jalan-masuk (MASUK property), None 1 frame, lalu taman->pintu (MASUK rumah).
    out = run([(0.0, {1: "jalan-masuk"}), (1.0, {}),               # None 1s < 3 -> grace
               (2.0, {1: "taman"}), (3.0, {1: "pintu"}), (9.0, {})])
    results.append(check("T7 kedip None < ambang tak menutup track", out, [
        {"kind": "MASUK property", "at": 0.0},
        {"kind": "MASUK rumah", "at": 3.0},
    ]))

    # T8) min_presence: blip phantom (umur < min_presence) -> SEMUA event track dibuang.
    #     Muncul di tepi (MASUK property) lalu hilang 0.5s kemudian.
    out = run([(0.0, {1: "jalan-masuk"}), (0.5, {}), (4.0, {})], min_presence_s=1.0)
    results.append(check("T8 blip < min_presence -> dibuang", out, []))

    # T8b) ...tapi track yang cukup umur tetap lolos (pasangan T8).
    out = run([(0.0, {1: "jalan-masuk"}), (1.0, {1: "jalan-masuk"}),
               (2.0, {1: "taman"}), (3.0, {1: "pintu"}), (9.0, {})], min_presence_s=1.0)
    results.append(check("T8b umur >= min_presence -> lolos", out, [
        {"kind": "MASUK property", "at": 0.0},
        {"kind": "MASUK rumah", "at": 3.0},
    ]))

    # T9) FINALISASI lewat gap kosong lalu TRACK BARU: tid1 di tepi lalu hilang
    #     (>=ambang) -> KELUAR property; tid2 (orang lain / ID baru) datang lalu hilang.
    #     Track-sentris: tid yang PERSISTEN lintas-gap = satu track (tak split); yang
    #     memisah = frame kosong >=ambang lalu ID baru.
    out = run([(0.0, {1: "jalan-masuk"}), (1.0, {1: "jalan-masuk"}),
               (5.0, {}),                    # 5-seen(1)=4 >= 3 -> finalisasi tid1 (KELUAR property@1)
               (6.0, {2: "jalan-masuk"}),    # ID baru -> MASUK property@6
               (10.0, {})])                  # 10-seen(6)=4 -> finalisasi tid2 (KELUAR property@6)
    results.append(check("T9 gap kosong finalisasi lalu ID baru", out, [
        {"kind": "MASUK property", "at": 0.0},
        {"kind": "KELUAR property", "at": 1.0},
        {"kind": "MASUK property", "at": 6.0},
        {"kind": "KELUAR property", "at": 6.0},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
