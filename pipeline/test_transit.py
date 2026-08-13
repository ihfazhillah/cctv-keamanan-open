"""Test TransitAggregator — passage se-arah berdekatan -> SATU trigger klip transit.

    uv run pipeline/test_transit.py

Nol-deps. Menjawab "terdeteksi keluar/masuk tapi tak ada video": satu keluar
(KELUAR rumah + KELUAR property) jadi SATU video, tanpa spam per-passage.

KONTRAK:
    class TransitAggregator:
        def __init__(self, emit_delay=6.0, join_gap=10.0)
        def feed(self, trigger)             # serap passage; non-passage diabaikan
        def due(self, t) -> list[trigger]   # transit yang hening >= emit_delay
        def flush(self) -> list[trigger]    # semua transit terbuka
    Trigger transit: {kind:'keluar'|'masuk', start, end, gates:[...], at:start}.
"""

from live import TransitAggregator


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def kel(gate, at): return {"kind": f"KELUAR {gate}", "at": at}
def mas(gate, at): return {"kind": f"MASUK {gate}", "at": at}


def main():
    r = []

    # T1) satu keluar: KELUAR rumah + property berdekatan -> satu transit, due setelah hening.
    a = TransitAggregator(emit_delay=6, join_gap=10)
    a.feed(kel("rumah", 100)); a.feed(kel("property", 101))
    r.append(check("T1 belum hening (<emit_delay) -> belum due", a.due(104), []))
    r.append(check("T1 due -> 1 transit keluar [100,101]", a.due(107), [
        {"kind": "keluar", "start": 100, "end": 101, "gates": ["property", "rumah"], "at": 100},
    ]))

    # T2) arah FLIP menutup transit sebelumnya: keluar dipancarkan, masuk mulai baru.
    a = TransitAggregator(emit_delay=6, join_gap=10)
    a.feed(kel("rumah", 100)); a.feed(mas("property", 103))
    r.append(check("T2 flip -> keluar seketika (ready), masuk belum", a.due(103), [
        {"kind": "keluar", "start": 100, "end": 100, "gates": ["rumah"], "at": 100},
    ]))
    r.append(check("T2 masuk due setelah hening", a.due(110), [
        {"kind": "masuk", "start": 103, "end": 103, "gates": ["property"], "at": 103},
    ]))

    # T3) jeda > join_gap antar keluar -> DUA transit terpisah.
    a = TransitAggregator(emit_delay=6, join_gap=10)
    a.feed(kel("rumah", 100)); a.feed(kel("rumah", 120))
    r.append(check("T3 jeda>join_gap -> dua transit", a.due(126), [
        {"kind": "keluar", "start": 100, "end": 100, "gates": ["rumah"], "at": 100},
        {"kind": "keluar", "start": 120, "end": 120, "gates": ["rumah"], "at": 120},
    ]))

    # T4) non-passage (close/loiter) diabaikan.
    a = TransitAggregator()
    a.feed({"kind": "close", "zone": "teras", "start": 1, "end": 2})
    a.feed({"kind": "loiter", "zone": "teras", "start": 1, "at": 31})
    r.append(check("T4 non-passage diabaikan", a.flush(), []))

    # T5) flush memancarkan transit terbuka (klip terakhir tak hilang saat shutdown).
    a = TransitAggregator(emit_delay=6)
    a.feed(kel("rumah", 100)); a.feed(kel("property", 101))
    r.append(check("T5 flush -> transit terbuka", a.flush(), [
        {"kind": "keluar", "start": 100, "end": 101, "gates": ["property", "rumah"], "at": 100},
    ]))

    print()
    if all(r):
        print(f"ALL PASS ({len(r)}/{len(r)})")
    else:
        print(f"FAIL {r.count(False)}/{len(r)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
