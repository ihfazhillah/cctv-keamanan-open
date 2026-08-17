"""Test SightingRecorder — catat SETIAP track masuk zona bernama sbg 'lewat',
walau deteksi BERKEDIP (absen sesaat). Kunci: kedip TIDAK mereset cooldown
(prune berbasis umur, bukan absen-sesaat) -> tak dobel. Regresi bug prune-presence.

    uv run pipeline/test_sighting.py
"""
import os
os.environ.setdefault("TG_TOKEN", "x")     # run_live import butuh env (module-level)
os.environ.setdefault("TG_CHAT_ID", "0")
os.environ.setdefault("CHAT_ID", "0")
from run_live import SightingRecorder


class FakeWriter:
    def __init__(self):
        self.events = []

    def tulis(self, **kw):
        self.events.append(kw)


def zonasp(w):
    return [(e["zone"], e["species"]) for e in w.events]


def check(name, cond):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
    return bool(cond)


def main():
    r = []

    # S1) sentuhan SEKEJAP + KEDIP: pintu(0) -> hilang(0.2) -> pintu(1.0).
    #     pintu tercatat SEKALI (cooldown 8s tak reset walau track kedip).
    w = FakeWriter(); s = SightingRecorder(w, cooldown_s=8.0)
    s.observe(0.0, {5: "pintu"}, {})
    s.observe(0.2, {}, {9: "teras"})      # tid5 kedip hilang; kucing tid9 muncul
    s.observe(1.0, {5: "pintu"}, {})      # tid5 balik pintu -> JANGAN dobel
    s.observe(2.0, {5: "taman"}, {})      # zona baru -> emit
    s.observe(3.0, {5: "jalan-utama"}, {})  # ABAI
    zs = zonasp(w)
    r.append(check("S1 pintu sekejap tercatat", ("pintu", None) in zs))
    r.append(check("S1 kucing teras tercatat", ("teras", "kucing") in zs))
    r.append(check("S1 zona baru (taman) tercatat", ("taman", None) in zs))
    r.append(check("S1 KEDIP tak dobel pintu (cooldown utuh)", zs.count(("pintu", None)) == 1))
    r.append(check("S1 jalan-utama diabaikan", all(z != "jalan-utama" for z, _ in zs)))
    r.append(check("S1 semua notify=1", all(e["notify"] == 1 for e in w.events)))

    # S2) setelah cooldown lewat, zona sama boleh emit lagi (mis. kunjungan baru).
    w = FakeWriter(); s = SightingRecorder(w, cooldown_s=8.0)
    s.observe(0.0, {7: "teras"}, {})
    s.observe(1.0, {7: "teras"}, {})      # diam -> zona==prev -> tak emit
    s.observe(20.0, {7: "teras"}, {})     # 20s > cooldown -> emit lagi? zona==prev (masih ingat) -> TIDAK
    r.append(check("S2 diam di zona sama -> emit sekali", zonasp(w).count(("teras", None)) == 1))

    # S3) track pindah zona lalu balik SETELAH cooldown -> emit lagi (bukan diam).
    w = FakeWriter(); s = SightingRecorder(w, cooldown_s=8.0)
    s.observe(0.0, {3: "pintu"}, {})
    s.observe(1.0, {3: "taman"}, {})
    s.observe(12.0, {3: "pintu"}, {})     # balik pintu, 12s > cooldown -> emit
    r.append(check("S3 balik zona sesudah cooldown -> emit", zonasp(w).count(("pintu", None)) == 2))

    print()
    if all(r):
        print(f"ALL PASS ({len(r)}/{len(r)})")
    else:
        print(f"FAIL {r.count(False)}/{len(r)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
