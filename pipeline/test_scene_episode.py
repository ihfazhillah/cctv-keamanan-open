"""Test SceneEpisode — bingkai episode level-scene dari occupancy zona (ID-free).

    uv run pipeline/test_scene_episode.py   (atau: python3 ...)

Nol-deps. Input = set nama zona per frame (BUKAN track_id). Menegaskan state machine
IDLE<->ACTIVE, penutupan lewat grace, pemotongan max_s, arah dari urutan zona, & flush.
"""
from live import SceneEpisode


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return ok


def run(frames, grace_s=3.0, max_s=100.0, flush_at=None):
    se = SceneEpisode(grace_s=grace_s, max_s=max_s)
    out = []
    for t, occ in frames:
        out += se.update(set(occ), t)
    if flush_at is not None:
        out += se.flush(flush_at)
    return out


def main():
    results = []

    # E1) MASUK penuh: jalan-masuk -> taman -> teras -> pintu, lalu sepi >= grace.
    #     Satu episode, arah=masuk, gates urut kemunculan.
    out = run([(0.0, {"jalan-masuk"}), (1.0, {"taman"}), (2.0, {"teras"}),
               (3.0, {"pintu"}), (4.0, {"pintu"}), (5.0, {}), (9.0, {})])
    results.append(check("E1 jalan-masuk->pintu lalu sepi: episode MASUK", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 4.0, "arah": "masuk",
         "gates": ["jalan-masuk", "taman", "teras", "pintu"],
         "zona": ["jalan-masuk", "pintu", "taman", "teras"]},
    ]))

    # E2) KELUAR: pintu -> teras -> taman -> jalan-masuk (kedalaman turun).
    out = run([(0.0, {"pintu"}), (1.0, {"teras"}), (2.0, {"taman"}),
               (3.0, {"jalan-masuk"}), (4.0, {}), (7.0, {})])
    results.append(check("E2 pintu->jalan-masuk: episode KELUAR", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 3.0, "arah": "keluar",
         "gates": ["pintu", "teras", "taman", "jalan-masuk"],
         "zona": ["jalan-masuk", "pintu", "taman", "teras"]},
    ]))

    # E3) KEDIP < grace TIDAK memotong: taman, kosong 1s, taman lagi -> tetap SATU episode.
    out = run([(0.0, {"taman"}), (1.0, {}), (2.0, {"taman"}),
               (3.0, {}), (4.0, {}), (5.0, {})])
    results.append(check("E3 kedip < grace -> satu episode (tak terpotong)", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 2.0, "arah": "lewat",
         "gates": ["taman"], "zona": ["taman"]},
    ]))

    # E4) SATU zona (diam) -> arah 'lewat' (tak cukup jejak untuk tentukan arah).
    out = run([(0.0, {"teras"}), (1.0, {"teras"}), (2.0, {}), (5.0, {})])
    results.append(check("E4 satu zona -> lewat", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 1.0, "arah": "lewat",
         "gates": ["teras"], "zona": ["teras"]},
    ]))

    # E5) POTONG max_s: hadir terus melewati max -> tutup lalu buka lagi (episode kedua).
    out = run([(t, {"taman"}) for t in range(0, 8)] + [(11.0, {})], grace_s=3.0, max_s=5.0)
    results.append(check("E5 durasi >= max_s -> dipotong jadi dua episode", out, [
        {"kind": "episode_mulai", "at": 0},
        {"kind": "episode", "start": 0, "end": 5, "arah": "lewat", "gates": ["taman"], "zona": ["taman"]},
        {"kind": "episode_mulai", "at": 5},
        {"kind": "episode", "start": 5, "end": 7, "arah": "lewat", "gates": ["taman"], "zona": ["taman"]},
    ]))

    # E6) FLUSH menutup episode yang masih terbuka (shutdown).
    out = run([(0.0, {"taman"}), (1.0, {"pintu"})], flush_at=1.0)
    results.append(check("E6 flush menutup episode terbuka", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 1.0, "arah": "masuk",
         "gates": ["taman", "pintu"], "zona": ["pintu", "taman"]},
    ]))

    # E7) IDLE tanpa occupancy -> tak ada event; flush pun kosong.
    out = run([(0.0, {}), (1.0, {}), (5.0, {})], flush_at=5.0)
    results.append(check("E7 tak ada orang -> tak ada event", out, []))

    # E8) jalan-utama = luar (depth 0) TETAP membingkai episode kalau kita masukkan?
    #     SceneEpisode netral thd nama zona; occupancy yang menyaring. Di sini kita uji
    #     bahwa dua kunjungan terpisah jeda >= grace jadi DUA episode.
    out = run([(0.0, {"taman"}), (1.0, {}), (2.0, {}), (3.0, {}), (4.0, {}),
               (5.0, {"taman"}), (6.0, {}), (9.0, {})], grace_s=3.0)
    results.append(check("E8 dua kunjungan (jeda >= grace) -> dua episode", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 0.0, "arah": "lewat", "gates": ["taman"], "zona": ["taman"]},
        {"kind": "episode_mulai", "at": 5.0},
        {"kind": "episode", "start": 5.0, "end": 5.0, "arah": "lewat", "gates": ["taman"], "zona": ["taman"]},
    ]))

    # E9) KELUAR via PUNCAK: teras -> pintu (ambang) -> taman. Titik-ujung
    #     se-kedalaman (teras=taman=2) tapi sempat menyentuh pintu=3 -> KELUAR,
    #     bukan 'lewat'. Regresi bug notif keluar-rumah luput (#2588).
    out = run([(0.0, {"teras"}), (1.0, {"pintu"}), (2.0, {"taman"}),
               (3.0, {}), (6.0, {})])
    results.append(check("E9 teras->pintu->taman: episode KELUAR (via puncak)", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 2.0, "arah": "keluar",
         "gates": ["teras", "pintu", "taman"],
         "zona": ["pintu", "taman", "teras"]},
    ]))

    # E10) DOMINAN DI LUAR: orang di jalan-utama (terhalang pagar), anchor separuh-badan
    #      jitter sekejap ke dekat-kolam. Kehadiran luar(4) > dalam(1) -> 'lewat', BUKAN
    #      masuk. Regresi false-positive #2691 (pelewat jalan dikira masuk properti).
    out = run([(0.0, {"jalan-utama"}), (1.0, {"jalan-utama"}), (2.0, {"dekat-kolam"}),
               (3.0, {"jalan-utama"}), (4.0, {"jalan-utama"}), (5.0, {}), (9.0, {})])
    results.append(check("E10 dominan jalan-utama + blip dekat-kolam -> lewat (bukan masuk)", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 4.0, "arah": "lewat",
         "gates": ["jalan-utama", "dekat-kolam"],
         "zona": ["dekat-kolam", "jalan-utama"]},
    ]))

    # E11) MASUK sah TETAP masuk: sebentar di jalan-utama lalu benar-benar masuk ke dalam
    #      (dalam menang) -> 'masuk' tak ikut tertekan aturan dominan-luar.
    out = run([(0.0, {"jalan-utama"}), (1.0, {"jalan-masuk"}), (2.0, {"taman"}),
               (3.0, {"teras"}), (4.0, {"pintu"}), (5.0, {"pintu"}), (6.0, {}), (10.0, {})])
    results.append(check("E11 jalan-utama sekejap lalu masuk dalam -> masuk", out, [
        {"kind": "episode_mulai", "at": 0.0},
        {"kind": "episode", "start": 0.0, "end": 5.0, "arah": "masuk",
         "gates": ["jalan-utama", "jalan-masuk", "taman", "teras", "pintu"],
         "zona": ["jalan-masuk", "jalan-utama", "pintu", "taman", "teras"]},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
