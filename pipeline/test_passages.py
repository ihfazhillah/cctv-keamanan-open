"""Test PassageTracker — RONDE 4: arah masuk/keluar (level PROPERTI, streaming).

    uv run pipeline/test_passages.py   (atau: python3 ...)

Nol-deps. Mengangkat rule detect_passages dari store.py ke bentuk STREAMING.

KONTRAK ronde-4:
    class PassageTracker:
        def __init__(self, ambang_s, gates=GATES, ignore=frozenset({"jalan-utama"}))
        def update(self, occupied_set, t) -> list[passage]

    GATES (lift dari store.py, taruh di live.py):
        [{"zones": {"teras"}, "lahir": "KELUAR rumah", "mati": "MASUK rumah"},
         {"zones": {"jalan-masuk", "tangga"}, "lahir": "MASUK property", "mati": "KELUAR property"}]

    DUA bagian detect_passages, sekarang STREAMING:
      1. merge-interval -> PROPERTY-PRESENCE tunggal yang berjalan (bukan sort daftar lengkap):
         property_zones = occupied_set - ignore
         - property_zones ADA:
             * kalau presence terbuka & (t - end) >= ambang_s -> TUTUP presence lama dulu (gap
               besar walau tanpa frame-kosong = tahan drop-stale/sparse), lalu BUKA presence baru
             * kalau presence terbuka & gap kecil -> perpanjang (end=t, last_zones=property_zones)
             * kalau tak ada presence -> buka (start=t, first_zones=property_zones)
         - property_zones KOSONG:
             * presence terbuka & (t - end) >= ambang_s -> TUTUP (emit passages)
      2. klasifikasi gate saat presence TUTUP (if..if, BUKAN elif -> satu presence bisa dua-duanya):
         - first_zones irisan gate.zones -> passage {"kind": gate["lahir"], "at": start}
         - last_zones  irisan gate.zones -> passage {"kind": gate["mati"],  "at": end}

    ambang_s = level PROPERTI (gabungan zona), BEDA dari exit_hysteresis EpisodeTracker (per-zona).
    = konsep ambang-dinaikkan-ke-properti. Klasifikasi = arah-masuk-keluar-lahir-mati-teras.

Frame di test = (t, [zona terisi]); P1-P8 pakai satu zona per frame (orang bergerak zona-ke-zona
berurutan). P9+ menguji frame MULTI-ZONA — kasus normal, bukan tepi: occupied_set zone-centric =
union semua zona yg punya >=1 deteksi, jadi dua orang di dua zona -> set berisi 2.
first/last dilacak sbg SET justru supaya jujur di kasus ini.

URUTAN keluaran (dipakukan oleh P3 & P9): loop gate-major sesuai daftar GATES, di tiap gate
`lahir` diperiksa sebelum `mati` (sama seperti store.py:37-41).

BATAS SADAR yg dipakukan P10: gate hanya dibaca dari TEPI presence (first_zones saat buka,
last_zones saat tutup). Zona gate yg muncul di TENGAH presence tidak memancarkan apa-apa.
Ini bukan kelalaian: kemunculan di tengah tidak membawa informasi ARAH (orang di teras bisa
sedang keluar ATAU sedang masuk), jadi memancarkan `lahir` di sana akan menambah false positive
"KELUAR rumah" pada setiap kedatangan tamu. Harga zone-centric; obatnya person/track-centric.
"""

from live import PassageTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames, ambang_s=3.0):
    tr = PassageTracker(ambang_s=ambang_s)
    out = []
    for t, occ in frames:
        out.extend(tr.update(set(occ), t))
    return out


def main():
    results = []

    # P1) KELUAR rumah: lahir di teras (first=teras), pergi ke taman (last=taman, bukan gate).
    out = run([(0.0, ["teras"]), (1.0, ["taman"]), (2.0, ["taman"]), (6.0, [])])
    results.append(check("P1 lahir di teras -> KELUAR rumah @start", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
    ]))

    # P2) MASUK rumah: lahir di taman (bukan gate), mati di teras (last=teras) -> MASUK @end.
    out = run([(0.0, ["taman"]), (1.0, ["taman"]), (2.0, ["teras"]), (6.0, [])])
    results.append(check("P2 mati di teras -> MASUK rumah @end", out, [
        {"kind": "MASUK rumah", "at": 2.0},
    ]))

    # P3) KELUAR lalu MASUK (keluar rumah, muter, balik): first=teras DAN last=teras -> DUA passage.
    #     Ini kasus footage nyata store.py (7 episode -> 1 presence -> KELUAR + MASUK). if..if.
    out = run([(0.0, ["teras"]), (1.0, ["taman"]), (2.0, ["dekat-kolam"]),
               (3.0, ["taman"]), (4.0, ["teras"]), (9.0, [])])
    results.append(check("P3 first=teras & last=teras -> KELUAR@start DAN MASUK@end (if..if)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK rumah", "at": 4.0},
    ]))

    # P4) MERGE lewat celah < ambang (ADA frame kosong di gap) -> SATU presence.
    #     teras end=1.0; taman muncul t=3.0 -> gap 2.0 < 3 -> merge (bukan 3.0=ambang yg split).
    out = run([(0.0, ["teras"]), (1.0, ["teras"]),
               (2.0, []),                          # kosong, gap 1s < 3 -> grace
               (3.0, ["taman"]), (4.0, ["taman"]),  # gap dari end(1.0)=2s < 3 -> merge
               (9.0, [])])
    results.append(check("P4 celah<=ambang -> satu presence (KELUAR@0)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
    ]))

    # P5) SPLIT lewat gap > ambang TANPA frame-kosong di antara (sparse/drop-stale):
    #     gap dicek saat AKTIVITAS muncul lagi -> DUA presence. (robustness inti live.)
    out = run([(0.0, ["teras"]), (1.0, ["taman"]),
               (6.0, ["teras"]), (7.0, ["taman"]),   # 6.0-1.0=5 >= 3 -> tutup presence-1 di sini
               (12.0, [])])
    results.append(check("P5 gap>ambang tanpa frame-kosong -> dua presence (dua KELUAR)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "KELUAR rumah", "at": 6.0},
    ]))

    # P6) IGNORE jalan-utama: hanya jalan-utama terisi -> properti KOSONG -> tak ada presence.
    out = run([(0.0, ["jalan-utama"]), (1.0, ["jalan-utama"]), (2.0, [])])
    results.append(check("P6 jalan-utama diabaikan -> tak ada passage", out, []))

    # P7) GATE PROPERTI: lahir di jalan-masuk -> MASUK property @start.
    out = run([(0.0, ["jalan-masuk"]), (1.0, ["taman"]), (5.0, [])])
    results.append(check("P7 lahir di jalan-masuk -> MASUK property @start", out, [
        {"kind": "MASUK property", "at": 0.0},
    ]))

    # P8) DEFERRED: properti kosong tapi < ambang -> presence MASIH terbuka -> belum ada passage.
    out = run([(0.0, ["teras"]), (1.0, ["teras"]), (2.0, []), (3.0, [])])  # gap 1s & 2s < 3
    results.append(check("P8 masih grace -> belum ada passage", out, []))

    # P9) MULTI-ZONA saat BUKA: dua orang, teras & jalan-masuk terisi di frame yang sama.
    #     first_zones = {"teras", "jalan-masuk"} -> beririsan dgn KEDUA gate -> dua passage @start.
    #     Urutan = urutan GATES (gate teras dulu, baru gate properti).
    out = run([(0.0, ["teras", "jalan-masuk"]), (1.0, ["taman"]), (5.0, [])])
    results.append(check("P9 first multi-zona -> dua gate menyala @start", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK property", "at": 0.0},
    ]))

    # P9b) MULTI-ZONA saat TUTUP: last_zones = {"teras", "tangga"} -> dua `mati` @end.
    #      first = {"taman"} (bukan gate) -> tak ada `lahir`.
    out = run([(0.0, ["taman"]), (1.0, ["taman"]), (2.0, ["teras", "tangga"]), (7.0, [])])
    results.append(check("P9b last multi-zona -> dua gate mati @end", out, [
        {"kind": "MASUK rumah", "at": 2.0},
        {"kind": "KELUAR property", "at": 2.0},
    ]))

    # P10) GATE TENGGELAM (batas sadar, lihat docstring): presence dibuka oleh teras, lalu tamu
    #      masuk lewat jalan-masuk di TENGAH presence (t=30) — presence tak pernah tutup karena
    #      properti tak pernah kosong, jadi jalan-masuk tak pernah jadi first_zones MAUPUN
    #      last_zones. "MASUK property" TIDAK dipancarkan. Test ini memaku perilaku ini supaya
    #      perubahan diam-diam ketahuan, BUKAN karena hasilnya ideal.
    #      CATATAN: jarak antar-frame WAJIB < ambang, kalau tidak P5 yang berlaku (gap >= ambang
    #      tanpa frame kosong = presence baru) dan ini berhenti menguji "di tengah presence".
    out = run([(0.0, ["teras"]), (1.0, ["taman"]), (2.0, ["taman"]),
               (3.0, ["jalan-masuk"]),             # tamu datang di tengah presence
               (4.0, ["taman"]), (9.0, [])])
    results.append(check("P10 gate di tengah presence tenggelam (batas zone-centric)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
    ]))

    # P11) SEKALI PANCAR: setelah presence tutup, state harus DIBERSIHKAN. Frame kosong terus
    #      berdatangan (~30x/detik di live) dan tiap kali (t - end) >= ambang tetap benar —
    #      tanpa pembersihan, passage yang sama terpancar berulang tanpa henti.
    out = run([(0.0, ["teras"]), (1.0, ["taman"]), (2.0, ["taman"]),
               (6.0, []), (7.0, []), (8.0, [])])   # tiga frame kosong: hanya yg pertama boleh memancarkan
    results.append(check("P11 tutup sekali -> pancar sekali (bukan tiap frame kosong)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
    ]))

    # P11b) ...tapi jangan over-koreksi jadi "tak pernah memancar lagi". Setelah bersih, presence
    #       BARU harus tetap bisa lahir & memancar. Pasangan P11: menutup jalan pintas "pasang
    #       flag sudah_pancar" yang mematikan tracker selamanya.
    out = run([(0.0, ["teras"]), (1.0, ["taman"]), (6.0, []), (7.0, []),
               (20.0, ["teras"]), (21.0, ["taman"]), (26.0, [])])
    results.append(check("P11b presence baru sesudah bersih -> memancar lagi", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "KELUAR rumah", "at": 20.0},
    ]))

    # P12) IGNORE yang sebenarnya: jalan-utama tidak boleh MEMBUKA atau MEMPERPANJANG presence.
    #      P6 lolos hanya karena kebetulan jalan-utama bukan anggota gate manapun — ia tidak
    #      menguji apa pun soal ignore. Di sini jalan-utama ramai 4 detik di antara dua orang:
    #        - dengan `occupied - ignore`: properti KOSONG t=2..5 -> presence A tutup,
    #          lalu presence B lahir di jalan-masuk -> MASUK property terdeteksi.
    #        - tanpa ignore: jalan-utama menjembatani keduanya jadi SATU presence,
    #          jalan-masuk jatuh di tengah -> MASUK property tenggelam (lihat P10).
    #      Bergantung pada perbaikan P11 (presence A harus benar-benar bersih sebelum B lahir).
    out = run([(0.0, ["teras"]), (1.0, ["taman"]),                        # A keluar rumah
               (2.0, ["jalan-utama"]), (3.0, ["jalan-utama"]),
               (4.0, ["jalan-utama"]), (5.0, ["jalan-utama"]),            # jalan umum ramai
               (6.0, ["jalan-masuk"]), (7.0, ["taman"]),                  # B masuk properti
               (12.0, [])])
    results.append(check("P12 jalan-utama tak membuka/menjembatani presence", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK property", "at": 6.0},
    ]))

    # P13) SE-GATE, DUA ZONA: {jalan-masuk, tangga} keduanya milik gate properti yang SAMA.
    #      Gate menyala berdasarkan IRISAN tidak-kosong -> SATU passage, bukan satu per anggota.
    #      P9 buta terhadap ini karena memilih teras+jalan-masuk (dua gate berbeda).
    #      Skenario nyata: dua orang di dua zona jalur-masuk yang bertetangga.
    out = run([(0.0, ["jalan-masuk", "tangga"]), (1.0, ["taman"]), (6.0, [])])
    results.append(check("P13 dua zona se-gate -> satu passage (irisan, bukan per-zona)", out, [
        {"kind": "MASUK property", "at": 0.0},
    ]))

    # P13b) sisi `mati`: {teras} & gate rumah cuma punya satu zona, jadi pakai gate properti lagi
    #       di sisi tutup — last_zones = {jalan-masuk, tangga} -> satu KELUAR property.
    out = run([(0.0, ["taman"]), (1.0, ["jalan-masuk", "tangga"]), (6.0, [])])
    results.append(check("P13b dua zona se-gate saat tutup -> satu passage", out, [
        {"kind": "KELUAR property", "at": 1.0},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
