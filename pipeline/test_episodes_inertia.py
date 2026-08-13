"""Test EpisodeTracker — RONDE 5: REM MASUK (enter_inertia / masa percobaan).

    uv run pipeline/test_episodes_inertia.py   (atau: python3 ...)

Nol-deps. Melengkapi rem yang hilang: sejak ronde-1 EpisodeTracker hanya punya rem KELUAR
(exit_hysteresis). Batch `events.py:100` punya dua — `hadir_beruntun >= enter_inertia` sebelum
episode diakui lahir. Lubang ini kelihatan di footage nyata 07-22: `close pintu 177.10 -> 177.10
(0.00s)` — satu frame deteksi melahirkan episode penuh.

KONTRAK:
    EpisodeTracker(exit_hysteresis=0.0, loiter_s=None, enter_inertia=0.0)
                                                        ^ BARU, di posisi TERAKHIR supaya
                                                          pemanggilan posisional lama tetap sah
    RuleEngine(exit_hysteresis=0.0, loiter_s=None, ambang_s=3.0,
               ignore=frozenset({"jalan-utama"}), enter_inertia=0.0)   # diteruskan apa adanya

    ATURAN:
    1. Zona yang baru terlihat menjadi KANDIDAT, bukan episode. Ia baru dilahirkan setelah
       terlihat terus-menerus selama >= enter_inertia detik.
    2. STEMPEL WAKTU menyatakan KAPAN TERJADI, bukan kapan kita tahu: `at` loiter =
       `start + loiter_s` (saat ambang terlampaui), BUKAN waktu frame yang mendeteksinya.
       Bedanya tak terlihat kalau frame kebetulan jatuh tepat di titik lewat — tapi di live
       drop-stale membuat jarak frame tak seragam. Syaratnya sendiri tak berubah dari ronde-3:
       `timestamp - start >= loiter_s`. KAPAN DIPERIKSA dan APA YANG DICAP adalah dua hal.
    3. `start` episode = SAAT PERTAMA TERLIHAT, bukan saat dikonfirmasi. Rem masuk soal
       keyakinan KITA, bukan soal kapan orangnya datang. (batch juga mundurkan: `start_frame =
       frame_idx - enter_inertia + 1`, events.py:123 — parity dg oracle wajib dijaga)
    4. Kandidat yang HILANG sebelum lolos masa percobaan langsung gugur, tanpa memancarkan
       apa-apa. exit_hysteresis TIDAK berlaku untuk kandidat — ia cuma punya satu nyawa.
    5. Sesudah episode lahir, celah pendek diurus exit_hysteresis SEPERTI BIASA; episode yang
       sudah diakui TIDAK pernah dilempar kembali ke masa percobaan.
    6. Kandidat dilacak PER ZONA, saling bebas.
    7. KONFIGURASI TAK WARAS DITOLAK DI MUKA: `loiter_s <= enter_inertia` -> ValueError di
       __init__. Artinya menyetel alarm berbunyi lebih cepat daripada kesediaan kita percaya.
       Dengan penolakan ini, "kandidat beralarm" (notifikasi tanpa kejadian, tanpa `close`,
       tanpa apa pun yang bisa dibuka untuk diperiksa) jadi MUSTAHIL — bukan sekadar tak
       diuji. Kontrak di pintu masuk mengalahkan perilaku diam-diam yang harus dihafal.
    8. KELAHIRAN ADALAH SALAH SATU MOMEN PEMERIKSAAN LOITER (usul user, I11b). Loiter
       diperiksa di DUA tempat dengan syarat yang identik: di frame kelahiran, dan di tiap
       frame sesudahnya (jalur lama ronde-3). Bukan penundaan — tak ada keadaan tambahan yang
       dijaga; `start` kandidat toh sudah tersimpan. Latch `alerted` yang menjaga agar tak
       berbunyi dua kali. Ini menutup kasus di mana ambang loiter jatuh tempo SELAMA masa
       percobaan: tanpa pemeriksaan di kelahiran, alarm itu hilang tanpa jejak.
    9. `enter_inertia=0.0` (default) = perilaku lama persis -> tiga test episode lama WAJIB
       tetap hijau (5/5, 6/6, 6/6), begitu juga test_rules 9/9.

    SATUAN = DETIK, bukan frame (batch pakai frame + fps). Di live, drop-stale membuang frame
    sehingga "10 frame" bukan durasi yang stabil; detik jujur apa adanya. Ini satu-satunya
    tempat versi streaming SENGAJA menyimpang dari batch.

TRADE-OFF yang harus disadari saat memilih angkanya (ini inti ronde-5, bukan kodenya):
    Di footage 07-22, episode nol-detik `pintu` yang ingin dibuang itu JUSTRU yang melahirkan
    `MASUK rumah`. Rem masuk menukar kedipan-palsu dengan kejadian-singkat-nyata. I8 memasang
    penjaganya: kehadiran panjang yang nyata (dekat-kolam 157 dtk, diverifikasi mata) tak boleh
    ikut tertelan.

CATATAN: `PassageTracker` TIDAK ikut terlindungi ronde ini — ia membaca occupied_set mentah,
jadi satu kedipan di zona gate masih bisa membuka presence dan memancarkan passage palsu.
Itu ronde tersendiri (durasi minimum presence), sengaja tidak dicampur ke sini.
"""

from live import EpisodeTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames, **kw):
    tr = EpisodeTracker(**kw)
    out = []
    for t, occ in frames:
        out.extend(tr.update(set(occ), t))
    return out


def main():
    results = []

    # I1) KEDIPAN: terlihat sekali lalu hilang -> tak ada episode sama sekali.
    #     Ini persis `close pintu 177.10 -> 177.10 (0.00s)` dari footage nyata.
    out = run([(0.0, ["pintu"]), (0.1, []), (5.0, [])], enter_inertia=1.0)
    results.append(check("I1 kedipan gugur di masa percobaan -> tak ada episode", out, []))

    # I2) LOLOS masa percobaan -> episode lahir, dan start MUNDUR ke saat pertama terlihat.
    #     Kalau start dipatok saat konfirmasi, yang keluar 1.0 bukan 0.0.
    out = run([(0.0, ["teras"]), (0.5, ["teras"]), (1.0, ["teras"]), (2.0, [])],
              enter_inertia=1.0)
    results.append(check("I2 lolos -> start mundur ke pertama terlihat (bukan saat konfirmasi)",
                         out, [{"kind": "close", "zone": "teras", "start": 0.0, "end": 1.0}]))

    # I3) RESET: kandidat yang sempat hilang mengulang masa percobaan dari nol.
    #     Percobaan pertama (0.0-0.5) mati di 0.9; yang lahir adalah percobaan KEDUA.
    out = run([(0.0, ["taman"]), (0.5, ["taman"]),
               (0.9, []),                                  # kandidat gugur di sini
               (1.0, ["taman"]), (1.5, ["taman"]), (2.0, ["taman"]),
               (3.0, [])], enter_inertia=1.0)
    results.append(check("I3 kandidat gugur -> masa percobaan mengulang dari nol",
                         out, [{"kind": "close", "zone": "taman", "start": 1.0, "end": 2.0}]))

    # I4) Sesudah LAHIR, celah pendek urusan exit_hysteresis — TIDAK kembali ke masa percobaan.
    #     Kalau salah, kemunculan di 3.0 dianggap kandidat baru lalu gugur di 7.0 -> NOL trigger.
    out = run([(0.0, ["teras"]), (1.0, ["teras"]),         # lahir di 1.0, start 0.0
               (2.0, []),                                   # celah 1.0 < hysteresis 2.0
               (3.0, ["teras"]),                            # kembali: bukan kandidat baru
               (7.0, [])],                                  # celah 4.0 >= 2.0 -> tutup
              enter_inertia=1.0, exit_hysteresis=2.0)
    results.append(check("I4 episode yang sudah lahir tak dilempar balik ke masa percobaan",
                         out, [{"kind": "close", "zone": "teras", "start": 0.0, "end": 3.0}]))

    # I5) LOITER diukur dari start yang sudah dimundurkan.
    #     Dari start 0.0 + loiter 2.0 -> menyala di 2.0. Kalau diukur dari konfirmasi (1.0),
    #     ia baru jatuh tempo di 3.0 — dan di 3.0 zona sudah kosong, jadi TAK PERNAH menyala.
    out = run([(0.0, ["taman"]), (1.0, ["taman"]), (2.0, ["taman"]), (3.0, [])],
              enter_inertia=1.0, loiter_s=2.0)
    results.append(check("I5 loiter dihitung dari start yang dimundurkan", out, [
        {"kind": "loiter", "zone": "taman", "start": 0.0, "at": 2.0},
        {"kind": "close", "zone": "taman", "start": 0.0, "end": 2.0},
    ]))

    # I6) Kandidat PER ZONA dan saling bebas: teras gugur, taman lolos.
    out = run([(0.0, ["teras", "taman"]), (0.5, ["taman"]), (1.0, ["taman"]), (2.0, [])],
              enter_inertia=1.0)
    results.append(check("I6 kandidat per zona, saling bebas",
                         out, [{"kind": "close", "zone": "taman", "start": 0.0, "end": 1.0}]))

    # I7) REGRESI: enter_inertia=0.0 (default) = perilaku lama persis, lahir di frame pertama.
    out = run([(0.0, ["teras"]), (1.0, [])])
    results.append(check("I7 default 0.0 -> perilaku lama (lahir seketika)",
                         out, [{"kind": "close", "zone": "teras", "start": 0.0, "end": 0.0}]))

    # I8) PENJAGA — skenario NYATA dari footage 07-22: dekat-kolam terisi 157 detik (sudah
    #     diverifikasi dengan mata: memang ada orang di sana). Rem masuk tak boleh menelannya,
    #     dan celah 2 detik di tengah tak boleh memecahnya jadi dua episode.
    frames = [(float(i), ["dekat-kolam"]) for i in range(0, 158)]
    frames[100] = (100.0, [])          # kedipan hilang...
    frames[101] = (101.0, [])          # ...2 detik, masih < exit_hysteresis 3.0
    frames.append((165.0, []))         # baru di sini benar-benar tutup
    out = run(frames, enter_inertia=1.0, exit_hysteresis=3.0)
    results.append(check("I8 kehadiran panjang nyata tetap utuh (satu episode 157 dtk)",
                         out, [{"kind": "close", "zone": "dekat-kolam",
                                "start": 0.0, "end": 157.0}]))

    # I9) KANDIDAT MATI TAK BOLEH JADI RANJAU. Kedipan di 0.0 gugur; orang baru benar-benar
    #     datang di 10.0. Kalau kandidat yang sudah mati masih tersimpan, kedatangan baru
    #     dianggap melanjutkannya -> episode ter-backdate 10 detik, dwell melar, loiter menyala
    #     seketika, klip dicari dari titik yang salah. (I1/I3 tak menjangkau ini: di sana
    #     kandidat mati MASIH di dalam jendela masa percobaan.)
    out = run([(0.0, ["teras"]),                     # kedipan
               (5.0, []),                            # gugur — dan harus benar-benar LENYAP
               (10.0, ["teras"]), (11.0, ["teras"]), # kunjungan sungguhan
               (20.0, [])], enter_inertia=1.0)
    results.append(check("I9 kandidat mati lenyap total (bukan ranjau utk kunjungan berikutnya)",
                         out, [{"kind": "close", "zone": "teras", "start": 10.0, "end": 11.0}]))

    # I10) FLUSH saat ada kandidat menggantung -> []. Kandidat belum pernah jadi kejadian, jadi
    #      shutdown tidak boleh melahirkannya. Awas: jangan sampai `None` ikut terbawa ke daftar
    #      — ia lolos pemeriksaan `is SENTINEL`, sampai ke handle, dan menulis baris `null`.
    tr = EpisodeTracker(enter_inertia=1.0)
    tr.update({"pintu"}, 0.0)
    results.append(check("I10 flush dg kandidat menggantung -> [] (bukan [None])", tr.flush(), []))

    # I11) KONFIGURASI TAK WARAS DITOLAK DI PINTU MASUK, bukan dibiarkan berperilaku aneh.
    #      `loiter_s <= enter_inertia` artinya: "bunyikan alarm setelah 1 dtk" sekaligus "saya
    #      belum percaya ada orang sebelum 5 dtk" — dua setelan yang saling bertentangan.
    #      Ditolak di __init__ -> seluruh kelas masalah "kandidat beralarm" jadi MUSTAHIL,
    #      bukan sekadar tidak diuji. (usul user: kontrak di awal > perilaku diam-diam)
    try:
        EpisodeTracker(enter_inertia=5.0, loiter_s=1.0)
        ditolak = False
    except ValueError:
        ditolak = True
    results.append(check("I11 loiter_s <= enter_inertia ditolak di __init__", ditolak, True))

    # I11b) KELAHIRAN = MOMEN PEMERIKSAAN LOITER (usul user). Konfigurasi SAH (loiter 2 dtk >
    #       masa percobaan 1 dtk), tapi frame renggang: 0.0 lalu 5.0 — di live drop-stale
    #       membuat jarak frame tak seragam, jadi ini bukan skenario karangan.
    #       Di t=5.0 episode baru diakui lahir, DAN ambang loiter (2 dtk) sudah lama terlampaui.
    #       Tanpa pemeriksaan di kelahiran, alarm itu hilang tanpa jejak: frame berikutnya
    #       zona sudah kosong, yang keluar cuma `close`.
    #       Stempelnya = saat ambang BENAR-BENAR terlampaui (2.0), bukan saat kita tahu (5.0).
    out = run([(0.0, ["taman"]), (5.0, ["taman"]), (6.0, [])],
              enter_inertia=1.0, loiter_s=2.0)
    results.append(check("I11b kelahiran = momen periksa loiter (frame renggang)", out, [
        {"kind": "loiter", "zone": "taman", "start": 0.0, "at": 2.0},
        {"kind": "close", "zone": "taman", "start": 0.0, "end": 5.0},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
