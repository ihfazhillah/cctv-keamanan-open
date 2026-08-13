"""Test EpisodeTracker — RONDE 2: exit_hysteresis (celah pendek tak memutus episode).

    uv run pipeline/test_episodes_hysteresis.py   (atau: python3 ...)

Nol-deps. Menambah SATU perilaku di atas ronde-1: exit_hysteresis.

KONTRAK ronde-2:
    EpisodeTracker(exit_hysteresis=0.0)
      exit_hysteresis = detik; 0.0 = tutup SEGERA saat zona kosong (= perilaku ronde-1).
    .update(occupied_set, t) -> list[trigger]
      - zona di occupied            -> buka (kalau baru) / perpanjang end=t
      - zona TERBUKA & TAK di occupied:
          (t - end) >= exit_hysteresis  -> TUTUP (emit close), hapus
          belum                          -> BIARKAN (grace: episode lanjut, celah diserap)
      - end = t TERAKHIR zona terlihat (bukan saat tutup)
      - cek-tutup PER-ZONA tiap frame (bukan cuma saat occupied kosong total)
        -> zona tutup independen (sekaligus memperbaiki isu #3 ronde-1)

REGRESI: EpisodeTracker() (default hysteresis=0) HARUS tetap lolos test_episodes.py.
Karena itu ronde-1 & ronde-2 = dua file test, dua-duanya wajib hijau.

Hysteresis diukur DETIK jam-dinding (t = time.time() di produksi) -> tahan drop-stale:
yang dinilai "sudah berapa lama TAK terlihat", bukan "berapa frame".
"""

from live import EpisodeTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(hysteresis, frames):
    tr = EpisodeTracker(exit_hysteresis=hysteresis)
    notes = []
    for t, occupied in frames:
        notes.extend(tr.update(set(occupied), t))
    return notes


def main():
    results = []

    # H1) CELAH PENDEK (0.2s) < hysteresis(1.0) -> DISERAP -> tetap SATU episode.
    #     end akhir = 0.8 (terakhir terlihat sebelum celah panjang terakhir).
    notes = run(1.0, [(0.0, ["teras"]), (0.2, ["teras"]),
                      (0.4, []),                       # celah 0.2s < 1.0 -> grace
                      (0.6, ["teras"]), (0.8, ["teras"]),
                      (3.0, [])])                      # celah 2.2s >= 1.0 -> tutup
    results.append(check("H1 celah pendek diserap -> satu episode",
                         notes, [{"kind": "close", "zone": "teras", "start": 0.0, "end": 0.8}]))

    # H2) CELAH PANJANG (>=hysteresis) MEMUTUS -> DUA episode.
    notes = run(1.0, [(0.0, ["teras"]), (0.2, ["teras"]),
                      (1.5, []),                       # 1.5-0.2=1.3 >= 1.0 -> tutup episode-1
                      (1.7, ["teras"]), (1.9, ["teras"]),
                      (4.0, [])])                      # 4.0-1.9=2.1 -> tutup episode-2
    results.append(check("H2 celah panjang -> dua episode", notes, [
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 0.2},
        {"kind": "close", "zone": "teras", "start": 1.7, "end": 1.9},
    ]))

    # H3) MASIH DALAM GRACE (kosong tapi < hysteresis) -> BELUM ada nota (episode terbuka).
    notes = run(1.0, [(0.0, ["teras"]), (0.5, ["teras"]),
                      (1.0, []), (1.2, [])])           # 0.5s & 0.7s sejak terlihat, dua-duanya < 1.0
    results.append(check("H3 masih grace -> belum ada nota", notes, []))

    # H4) PER-ZONA + hysteresis: teras ditinggal saat taman lanjut -> teras tutup independen
    #     setelah gap-nya sendiri lewat ambang (bukan menunggu taman kosong).
    notes = run(1.0, [(0.0, ["teras"]),
                      (0.2, ["teras", "taman"]),
                      (0.4, ["taman"]),                # teras hilang (gap mulai dari end 0.2)
                      (2.0, ["taman"]),                # 2.0-0.2=1.8 >= 1.0 -> tutup teras DI SINI
                      (3.5, [])])                      # 3.5-2.0=1.5 -> tutup taman
    results.append(check("H4 per-zona: teras tutup independen dari taman", notes, [
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 0.2},
        {"kind": "close", "zone": "taman", "start": 0.2, "end": 2.0},
    ]))

    # H5) REGRESI: hysteresis=0.0 -> tutup SEGERA saat kosong (= ronde-1).
    notes = run(0.0, [(0.0, ["teras"]), (0.2, ["teras"]), (0.4, [])])
    results.append(check("H5 hysteresis=0 -> tutup segera (perilaku ronde-1)",
                         notes, [{"kind": "close", "zone": "teras", "start": 0.0, "end": 0.2}]))

    # H6) ZONA PERSISTEN TAK BOLEH MENYANDERA PENUTUPAN zona lain.
    #     = kamera depan-garasi (MISSION): 'jalan' TAK PERNAH kosong (lalu-lalang jalan umum),
    #     'teras' muncul lalu ditinggal. teras HARUS tetap tutup walau occupied tak pernah kosong.
    #     (Menutup lubang: cek-tutup harus PER-ZONA tiap frame, BUKAN digerbang 'occupied kosong'.)
    notes = run(1.0, [(0.0, ["jalan"]),
                      (0.5, ["jalan", "teras"]),     # teras muncul
                      (1.0, ["jalan", "teras"]),     # teras end=1.0
                      (1.5, ["jalan"]),              # teras ditinggal; jalan tetap ramai
                      (2.5, ["jalan"]),              # 2.5-1.0=1.5 >= 1.0 -> teras HARUS tutup DI SINI
                      (5.0, ["jalan"])])             # jalan masih ramai; tak boleh ada nota lagi
    results.append(check("H6 zona persisten tak menyandera penutupan zona lain",
                         notes, [{"kind": "close", "zone": "teras", "start": 0.5, "end": 1.0}]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
