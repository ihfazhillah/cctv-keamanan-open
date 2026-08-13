"""Test EpisodeTracker — Piece #3, potongan 'menimbang' (update_episodes) — RONDE 1.

    uv run pipeline/test_episodes.py      (atau: python3 pipeline/test_episodes.py)

Nol-deps (murni logika). TAK butuh cv2/GPU/RTSP -> deteksi DIPALSUKAN sebagai
'himpunan zona yang terisi frame ini'. Ini yang bikin logika episode bisa dicek satu-satu.

KONTRAK (ronde 1 — sengaja MINIMAL):
    class EpisodeTracker:
        def update(self, occupied, t) -> list[trigger]
          occupied = set nama zona terisi frame ini, mis. {"teras"} atau set() kalau kosong
          t        = detik jam-dinding (float)
          return   = daftar nota lahir frame ini; KEBANYAKAN []; saat episode TUTUP -> 1 nota:
                     {"kind": "close", "zone": <str>, "start": <t_buka>, "end": <t_terakhir_terlihat>}

    Perilaku ronde 1 (HANYA ini):
      - zona mulai terisi        -> BUKA episode (state internal), TAK ada nota
      - zona tetap terisi        -> episode tetap satu, TAK ada nota
      - zona jadi kosong         -> TUTUP episode -> emit 1 nota close
                                    start = saat buka; end = t TERAKHIR zona masih terisi
      - tiap zona berdiri sendiri (teras & taman dilacak terpisah)

    BELUM di ronde ini (jangan diimplement dulu): exit_hysteresis (celah pendek tak menutup),
    loitering/notif-dini, masuk/keluar. Ronde-ronde berikutnya.

State hidup di dalam objek -> tiap skenario pakai EpisodeTracker() BARU (isolasi bersih),
sama seperti StatusMachine per-zona di events.py.
"""

from live import EpisodeTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames):
    """Umpankan urutan (t, occupied) ke EpisodeTracker BARU, kumpulkan SEMUA nota."""
    tr = EpisodeTracker()
    notes = []
    for t, occupied in frames:
        notes.extend(tr.update(set(occupied), t))
    return notes


def main():
    results = []

    # A) ZONA KOSONG TERUS -> tak pernah ada episode -> tak ada nota.
    notes = run([(0.0, []), (0.2, []), (0.4, [])])
    results.append(check("kosong terus -> tak ada nota", notes, []))

    # B) MUNCUL & TETAP (belum pergi) -> episode BUKA tapi belum tutup -> belum ada nota.
    #    (buka TIDAK meng-emit; hanya tutup yang meng-emit.)
    notes = run([(1.0, ["teras"]), (1.2, ["teras"]), (1.4, ["teras"])])
    results.append(check("muncul & tetap -> belum ada nota (buka tak emit)", notes, []))

    # C) MUNCUL, TETAP, PERGI -> saat kosong, TUTUP -> 1 nota.
    #    start = 2.0 (saat muncul), end = 2.4 (t TERAKHIR terlihat, BUKAN 2.6 saat kosong).
    notes = run([(2.0, ["teras"]), (2.2, ["teras"]), (2.4, ["teras"]), (2.6, [])])
    results.append(check("muncul->pergi -> 1 nota close (start=muncul, end=terakhir-terlihat)",
                         notes, [{"kind": "close", "zone": "teras", "start": 2.0, "end": 2.4}]))

    # D) DUA KUNJUNGAN TERPISAH -> DUA episode -> DUA nota.
    #    (ronde 1 tanpa hysteresis: celah sekecil apa pun memisah -> 2 episode. Hysteresis nanti.)
    notes = run([(3.0, ["teras"]), (3.2, []), (3.4, ["teras"]), (3.6, [])])
    results.append(check("dua kunjungan -> dua nota close", notes, [
        {"kind": "close", "zone": "teras", "start": 3.0, "end": 3.0},
        {"kind": "close", "zone": "teras", "start": 3.4, "end": 3.4},
    ]))

    # E) DUA ZONA BERDIRI SENDIRI -> teras & taman buka/tutup independen (satu tutup per frame).
    notes = run([(4.0, ["teras"]),
                 (4.2, ["teras", "taman"]),
                 (4.4, ["taman"]),          # teras hilang -> tutup teras [4.0, 4.2]
                 (4.6, [])])                # taman hilang -> tutup taman [4.2, 4.4]
    results.append(check("dua zona independen -> tutup masing-masing", notes, [
        {"kind": "close", "zone": "teras", "start": 4.0, "end": 4.2},
        {"kind": "close", "zone": "taman", "start": 4.2, "end": 4.4},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
