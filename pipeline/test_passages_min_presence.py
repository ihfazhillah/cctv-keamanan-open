"""Test PassageTracker — RONDE 5: DURASI MINIMUM PRESENCE (rem masuk level PROPERTI).

    uv run pipeline/test_passages_min_presence.py   (atau: python3 ...)

Nol-deps. Regresi ronde-4 tetap harus hijau (jalankan test_passages.py juga).

MASALAH yang ditambal:
    PassageTracker membaca `occupied` MENTAH, tak terlindung rem masuk (beda dari
    EpisodeTracker yang sudah punya enter_inertia). Satu KEDIPAN di zona gate -> presence
    hidup 1 frame -> first == last == {teras} -> aturan if..if memancarkan DUA passage palsu
    (`KELUAR rumah` + `MASUK rumah`) pada detik yang SAMA. (Lihat P3: first=teras & last=teras
    memang boleh dua-duanya; yang salah di sini bukan aturannya, tapi bahwa kedipan sesaat pun
    lolos jadi "presence".)

KONTRAK ronde-5 (parameter di posisi TERAKHIR, default = perilaku lama):
    class PassageTracker:
        def __init__(self, ambang_s, ignore=frozenset({"jalan-utama"}), min_presence_s=0.0)

    Perubahan HANYA di `tutup()`: presence yang bertahan kurang dari min_presence_s tidak
    memancarkan apa pun (kembalikan []), TAPI state tetap dibersihkan (presence = None).

    Ukuran durasi = end - start, dalam DETIK (bukan frame). Alasan sama seperti enter_inertia
    ronde-5 EpisodeTracker: di live drop-stale membuang frame, jadi "N frame" bukan durasi
    stabil; time.time() mengalir apa adanya. Batas pakai >= (dur == min -> SELAMAT), konsisten
    dengan loiter/enter_inertia.

    ambang_s   = berapa lama properti boleh KOSONG sebelum presence ditutup (rem KELUAR/ekor).
    min_presence_s = berapa lama presence harus TERISI sebelum dianggap kejadian (rem MASUK/awal).
    Dua sumbu berlawanan (kosong vs terisi) -> tak ada kombinasi mustahil -> TAK perlu guard
    __init__ (beda dari loiter_s <= enter_inertia yang memang kontradiksi).

TRADEOFF SADAR (bukan rem gratis):
    Ini menukar kedipan-palsu dengan RISIKO menelan kunjungan-singkat-NYATA. Di footage 07-22,
    `MASUK rumah @177,10` yang asli SELAMAT justru karena tracker ini polos (beberapa frame
    `pintu` memperpanjang presence). min_presence_s yang terlalu tinggi akan MEMBUNUHNYA juga.
    M4/M5 memaku dua sisi jurang itu; angka finalnya dipilih DARI DATA (events-live.jsonl),
    bukan ditebak — sama seperti enter_inertia.

BATAS JUJUR (= konsep durasi-episode-bukan-penampakan-menerus):
    Durasi = end - start = RENTANG presence, BUKAN "terlihat terus-menerus". Dua kedipan yang
    terpisah < ambang_s dijahit jadi satu presence ber-rentang lebar dan bisa LOLOS rem ini —
    tak bisa dibedakan dari kehadiran nyata sepanjang itu. min_presence_s menghajar kedipan
    TUNGGAL (rentang 0), bukan hantu-berkedip-berulang. Obat penuh untuk itu = person-centric.
"""

from live import PassageTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames, ambang_s=3.0, min_presence_s=0.0):
    tr = PassageTracker(ambang_s=ambang_s, min_presence_s=min_presence_s)
    out = []
    for t, occ in frames:
        out.extend(tr.update(set(occ), t))
    return out


def main():
    results = []

    # M1) BUG INTI: kedipan tunggal di teras (hidup 1 frame) -> presence dur 0 -> TAK memancar.
    #     Tanpa rem, ini memancarkan KELUAR@0 + MASUK@0 (dua passage palsu sedetik).
    out = run([(0.0, ["teras"]), (5.0, [])], min_presence_s=1.0)
    results.append(check("M1 kedipan tunggal -> [] (bug dua-passage-palsu mati)", out, []))

    # M2) PENJAGA: presence NYATA yang bertahan (dur 2s) >= min -> tetap memancar penuh.
    #     Rem tak boleh menelan kejadian sungguhan.
    #     (Catatan batas: tracker tak bisa membedakan ini dari dua kedipan berjarak 2s — lihat
    #      docstring "durasi = rentang, bukan penampakan-menerus".)
    out = run([(0.0, ["teras"]), (2.0, ["teras"]), (7.0, [])], min_presence_s=1.0)
    results.append(check("M2 presence dur 2 >= min -> KELUAR@0 & MASUK@2 selamat", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK rumah", "at": 2.0},
    ]))

    # M3) DEFAULT 0.0 = perilaku lama TAK berubah: kedipan yang sama tetap memancar dua-duanya.
    #     (Pemanggilan posisional lama PassageTracker(ambang_s, ignore) tetap sah.)
    out = run([(0.0, ["teras"]), (5.0, [])], min_presence_s=0.0)
    results.append(check("M3 default 0.0 -> perilaku lama (dua passage utuh)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK rumah", "at": 0.0},
    ]))

    # M4) BATAS: durasi PERSIS == min -> SELAMAT. Memaku >= (bukan >).
    out = run([(0.0, ["teras"]), (1.0, ["teras"]), (6.0, [])], min_presence_s=1.0)
    results.append(check("M4 dur == min -> selamat (paku >=)", out, [
        {"kind": "KELUAR rumah", "at": 0.0},
        {"kind": "MASUK rumah", "at": 1.0},
    ]))

    # M5) SISI TRADEOFF: durasi tepat DI BAWAH min -> mati. Ini SISI MAHAL rem — kunjungan
    #     singkat-tapi-nyata (spt MASUK @177,10) ikut hilang bila ambang kelewat tinggi.
    out = run([(0.0, ["teras"]), (0.5, ["teras"]), (6.0, [])], min_presence_s=1.0)
    results.append(check("M5 dur 0.5 < min -> [] (kunjungan singkat ikut mati)", out, []))

    # M6) DUA PRESENCE beruntun: yang pendek (dur 0) MATI, yang panjang (dur 1) SELAMAT, dan
    #     state harus BERSIH di antara (kalau tidak, sisa presence-1 bocor ke presence-2).
    #     Menggabungkan rem + penjaga + kebersihan-state (P11) dalam satu jalan.
    out = run([(0.0, ["teras"]), (5.0, ["teras"]), (6.0, ["teras"]), (11.0, [])], min_presence_s=1.0)
    results.append(check("M6 pendek mati + panjang selamat + state bersih", out, [
        {"kind": "KELUAR rumah", "at": 5.0},
        {"kind": "MASUK rumah", "at": 6.0},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
