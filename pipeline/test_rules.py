"""Test RuleEngine — perakit: EpisodeTracker (zona) + TrackPassageTracker (per-track).

    uv run pipeline/test_rules.py   (atau: python3 ...)

Nol-deps. Ini BUKAN tracker baru — ini perakit. Sejak passages naik ke track-sentris,
RuleEngine menyuapi DUA pandangan dari SATU sumber deteksi:
    - occupied_set  -> EpisodeTracker  (per-zona: "ada orang di zona X")
    - track_zones   -> TrackPassageTracker (per-badan: arah masuk/keluar)
Di produksi keduanya dihitung sekali dari dets yang sama (run_live.zona_per_track);
di test kita turunkan occupied dari track_zones supaya satu input menggerakkan keduanya.

KONTRAK:
    class RuleEngine:
        def __init__(self, exit_hysteresis=0.0, loiter_s=None, ambang_s=3.0,
                     ignore=frozenset({"jalan-utama"}), enter_inertia=0.0, min_presence_s=0.0)
        def update(self, occupied, track_zones, t) -> list[trigger]
        def flush(self) -> list[trigger]

    FAN-OUT, bukan rantai: passages TIDAK memakan keluaran episodes.
    URUTAN dalam satu panggilan: trigger EPISODE dulu (per-zona, detail), baru PASSAGE. Dipaku R1.
    DUA PANDANGAN (R3): `ignore` milik passages saja — jalan-utama tetap melahirkan episode
      tapi tak pernah jadi track "grounded" (depth 0). Jangan saring occupied sebelum fan-out.
    JAM MUNDUR (R6): t < t sebelumnya -> frame DIABAIKAN oleh KEDUA tracker sekaligus.
    FLUSH (R4/R5): dipanggil saat shutdown, harus IDEMPOTEN.

Bentuk trigger:
    episode : {"kind": "close", "zone":..., "start":..., "end":...}
              {"kind": "loiter", "zone":..., "start":..., "at":...}
    passage : {"kind": "MASUK rumah"|"KELUAR rumah"|"MASUK property"|"KELUAR property", "at":...}
"""

from live import RuleEngine


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def occ_of(track_zones):
    return {z for z in track_zones.values() if z is not None}


def run(frames, **kw):
    eng = RuleEngine(**kw)
    out = []
    for t, tz in frames:
        out.extend(eng.update(occ_of(tz), tz, t))
    return out, eng


def main():
    results = []

    # R1) FAN-OUT + URUTAN: satu badan taman->pintu lalu lenyap. MASUK rumah muncul saat
    #     finalisasi (mati di pintu); di panggilan itu close-episode (pintu) terpancar
    #     SEBELUM passage (MASUK rumah). Urutan episode-dulu tetap dipaku.
    out, _ = run([(0.0, {1: "taman"}), (1.0, {1: "pintu"}), (6.0, {})])
    results.append(check("R1 fan-out: episode dulu, passage kemudian (satu panggilan)", out, [
        {"kind": "close", "zone": "taman", "start": 0.0, "end": 0.0},
        {"kind": "close", "zone": "pintu", "start": 1.0, "end": 1.0},
        {"kind": "MASUK rumah", "at": 1.0},
    ]))

    # R2) loiter ikut lewat. taman bukan batas apa pun -> tak ada passage; murni jalur episode.
    out, _ = run([(0.0, {1: "taman"}), (1.0, {1: "taman"}), (2.0, {1: "taman"}), (7.0, {})],
                 loiter_s=2.0)
    results.append(check("R2 loiter diteruskan apa adanya", out, [
        {"kind": "loiter", "zone": "taman", "start": 0.0, "at": 2.0},
        {"kind": "close", "zone": "taman", "start": 0.0, "end": 2.0},
    ]))

    # R3) DUA PANDANGAN: jalan-utama melahirkan episode (kita ingin tahu ada orang di jalan)
    #     tapi TIDAK jadi track grounded (depth 0) -> tak ada passage. `ignore` milik passages saja.
    out, _ = run([(0.0, {1: "jalan-utama"}), (1.0, {1: "jalan-utama"}), (2.0, {}), (6.0, {})])
    results.append(check("R3 jalan-utama: episode YA, passage TIDAK", out, [
        {"kind": "close", "zone": "jalan-utama", "start": 0.0, "end": 1.0},
    ]))

    # R4) FLUSH saat shutdown: episode terbuka harus keluar. Badan yang masih di teras saat
    #     shutdown -> TIDAK menebak passage (interior rumah tak teramati) -> episode saja.
    out, eng = run([(0.0, {1: "teras"}), (1.0, {1: "teras"})])
    results.append(check("R4 sebelum flush: belum ada apa-apa", out, []))
    results.append(check("R4b flush memancarkan episode terbuka (passage tak menebak)", eng.flush(), [
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 1.0},
    ]))

    # R5) IDEMPOTEN: flush kedua tidak boleh memancarkan ulang.
    results.append(check("R5 flush kedua -> kosong", eng.flush(), []))

    # R6) JAM MUNDUR: frame dg t lebih kecil DIABAIKAN oleh kedua tracker. Hasil identik R1.
    out, _ = run([(0.0, {1: "taman"}), (1.0, {1: "pintu"}),
                  (0.5, {1: "dekat-kolam"}),        # jam mundur (NTP sinkron)
                  (6.0, {})])
    results.append(check("R6 t mundur diabaikan (bukan cuma satu tracker)", out, [
        {"kind": "close", "zone": "taman", "start": 0.0, "end": 0.0},
        {"kind": "close", "zone": "pintu", "start": 1.0, "end": 1.0},
        {"kind": "MASUK rumah", "at": 1.0},
    ]))

    # R7) KELUAR lewat engine: muncul di pintu, turun ke taman lalu naik ke pintu lagi
    #     (pernah ke halaman SESUDAH pintu -> arah KELUAR). Episode per-zona utuh; passage
    #     KELUAR rumah keluar saat finalisasi (arah dari lintasan, bukan lahir/mati mentah).
    out, _ = run([(0.0, {1: "pintu"}), (1.0, {1: "taman"}), (2.0, {1: "pintu"}), (7.0, {})])
    results.append(check("R7 keluar lewat engine: episode utuh + passage KELUAR (lintasan)", out, [
        {"kind": "close", "zone": "pintu", "start": 0.0, "end": 0.0},
        {"kind": "close", "zone": "taman", "start": 1.0, "end": 1.0},
        {"kind": "close", "zone": "pintu", "start": 2.0, "end": 2.0},
        {"kind": "KELUAR rumah", "at": 0.0},
    ]))

    # R8) flush pada engine yang belum pernah melihat apa pun -> [] (bukan crash).
    results.append(check("R8 flush mesin kosong -> []", RuleEngine().flush(), []))

    # R9) SEMBUH: kedipan satu frame di teras. enter_inertia menahan episode; track-sentris
    #     menahan passage (muncul+hilang di teras = tak menebak). DULU zone-centric memancarkan
    #     "KELUAR rumah" DAN "MASUK rumah" palsu dari satu kedipan (batas yang dipaku R9 lama).
    #     Kini: NOL notifikasi palsu.
    out, _ = run([(0.0, {1: "teras"}), (0.1, {}), (5.0, {})], enter_inertia=1.0)
    results.append(check("R9 kedipan teras -> nol passage palsu (sembuh dari zone-centric)", out, []))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
