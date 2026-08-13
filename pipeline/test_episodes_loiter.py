"""Test EpisodeTracker — RONDE 3: loitering / notif-dini (momen pancar KEDUA di tengah).

    uv run pipeline/test_episodes_loiter.py   (atau: python3 ...)

Nol-deps. Menambah SATU perilaku di atas ronde-1/2: alert DINI saat dwell lewat ambang.

KONTRAK ronde-3:
    EpisodeTracker(exit_hysteresis=0.0, loiter_s=None)
      loiter_s = ambang DWELL (detik). None = MATI (default) -> regresi aman.
      (CATATAN: default None, BUKAN 0.0 — kalau 0.0, dwell>=0 selalu benar -> loiter
       nyala tiap frame. None = tak pernah loiter.)
    .update(occupied_set, t) -> list[trigger]
      - zona HADIR & (t - start) >= loiter_s & BELUM alerted:
            emit loiter SEKALI, set alerted=True (fire-once per kunjungan)
      - nota loiter = {"kind": "loiter", "zone": <z>, "start": <mulai episode>, "at": <t saat lewat>}
      - nota close TETAP {"kind": "close", "zone", "start", "end"} — latch 'alerted' INTERNAL,
        JANGAN bocor ke nota (hati-hati kalau pakai **spread dict episode).

    Dua dial beda tugas (konsep loitering-vs-enter-inertia):
      enter_inertia = apakah NYATA (vs noise kedip) — belum di EpisodeTracker
      loiter_s      = apakah BERARTI (vs transient lewat) — INI ronde-3
    Orang bisa buka episode (nyata) tapi tak lewati loiter (jalan terus) -> tak alert.

REGRESI: loiter_s=None (default) -> test_episodes.py & test_episodes_hysteresis.py tetap hijau.
"""

from live import EpisodeTracker


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run(frames, exit_hysteresis=0.0, loiter_s=None):
    tr = EpisodeTracker(exit_hysteresis=exit_hysteresis, loiter_s=loiter_s)
    notes = []
    for t, occupied in frames:
        notes.extend(tr.update(set(occupied), t))
    return notes


def main():
    results = []

    # L1) DWELL LEWAT AMBANG -> loiter di tengah (t=3), lalu close di akhir. Urut: loiter dulu.
    notes = run([(0.0, ["teras"]), (1.0, ["teras"]), (2.0, ["teras"]),
                 (3.0, ["teras"]),                 # dwell 3.0 >= 3 -> loiter@3
                 (4.0, [])],                       # pergi -> close
                loiter_s=3.0)
    results.append(check("L1 dwell lewat ambang -> loiter (tengah) lalu close (akhir)", notes, [
        {"kind": "loiter", "zone": "teras", "start": 0.0, "at": 3.0},
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 3.0},
    ]))

    # L2) FIRE-ONCE — berlama jauh lewat ambang -> loiter TETAP sekali.
    notes = run([(0.0, ["teras"]), (3.0, ["teras"]),   # loiter@3
                 (4.0, ["teras"]), (5.0, ["teras"]), (6.0, ["teras"]),  # dwell besar, TAK ulang
                 (7.0, [])],
                loiter_s=3.0)
    results.append(check("L2 fire-once -> loiter sekali walau berlama", notes, [
        {"kind": "loiter", "zone": "teras", "start": 0.0, "at": 3.0},
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 6.0},
    ]))

    # L3) KUNJUNGAN SINGKAT (dwell < ambang) -> TAK ada loiter, hanya close. = transient lewat.
    notes = run([(0.0, ["teras"]), (1.0, ["teras"]), (2.0, [])], loiter_s=3.0)
    results.append(check("L3 kunjungan singkat -> tak loiter, hanya close", notes, [
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 1.0},
    ]))

    # L4) LATCH BERTAHAN LEWAT GRACE GAP — oklusi (celah < hysteresis) TAK memicu loiter ulang.
    #     = exit_hysteresis serap oklusi -> tak double-alert (konsep user).
    notes = run([(0.0, ["teras"]), (1.0, ["teras"]), (2.0, ["teras"]), (3.0, ["teras"]),  # loiter@3
                 (4.0, []),                        # oklusi: gap 4-3=1 < 2 -> grace, alerted tetap
                 (5.0, ["teras"]), (6.0, ["teras"]),   # muncul lagi, dwell besar TAPI alerted -> tak ulang
                 (9.0, [])],                       # gap 9-6=3 >= 2 -> close
                exit_hysteresis=2.0, loiter_s=3.0)
    results.append(check("L4 latch bertahan lewat oklusi -> loiter tak ulang", notes, [
        {"kind": "loiter", "zone": "teras", "start": 0.0, "at": 3.0},
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 6.0},
    ]))

    # L5) KUNJUNGAN BARU RE-ARM — setelah close, kunjungan berikut BISA loiter lagi (alerted fresh).
    notes = run([(0.0, ["teras"]), (3.0, ["teras"]), (4.0, []),   # kunjungan-1: loiter@3, close(0,3)
                 (5.0, ["teras"]), (8.0, ["teras"]), (9.0, [])],  # kunjungan-2: loiter@8, close(5,8)
                loiter_s=3.0)
    results.append(check("L5 kunjungan baru re-arm -> loiter lagi", notes, [
        {"kind": "loiter", "zone": "teras", "start": 0.0, "at": 3.0},
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 3.0},
        {"kind": "loiter", "zone": "teras", "start": 5.0, "at": 8.0},
        {"kind": "close", "zone": "teras", "start": 5.0, "end": 8.0},
    ]))

    # L6) REGRESI — loiter_s=None (default) -> TAK ada nota loiter, cuma close (perilaku lama).
    notes = run([(0.0, ["teras"]), (1.0, ["teras"]), (2.0, ["teras"]),
                 (3.0, ["teras"]), (4.0, ["teras"]), (5.0, [])])   # loiter_s default None
    results.append(check("L6 loiter_s=None -> tak ada loiter (regresi)", notes, [
        {"kind": "close", "zone": "teras", "start": 0.0, "end": 4.0},
    ]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
