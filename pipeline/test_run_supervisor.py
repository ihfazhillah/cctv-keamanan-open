"""Test nol-deps utk logika supervisor (build cmd + desired_procs)."""
from run_supervisor import build_taman_cmd, build_garasi_cmd, desired_procs, stream_url


def test_build_taman_cmd():
    cam = {"nama": "taman", "stream": "taman", "zone_file": "zones-taman.json",
           "model": "yolo26l.pt", "conf": 0.15, "loiter_s": 30}
    c = build_taman_cmd(cam)
    assert "run_live.py" in c[1]
    assert stream_url("taman") in c
    assert "--zone-file" in c and "zones-taman.json" in c
    assert "--model" in c and "yolo26l.pt" in c
    assert "--conf" in c and "0.15" in c
    assert "--loiter_s" in c and "30" in c


def test_build_taman_cmd_defaults():
    c = build_taman_cmd({"nama": "x", "stream": "x"})       # tanpa loiter_s -> flag absen
    assert "--loiter_s" not in c
    assert "zones.json" in c and "yolo26l.pt" in c


def test_desired_satu_taman_satu_garasi():
    cfg = {"kamera": [
        {"nama": "taman", "stream": "taman", "peran": "taman-penuh", "enabled": True},
        {"nama": "garasi", "stream": "garasi", "peran": "garasi-ringan", "enabled": True},
        {"nama": "gudang", "stream": "gudang", "peran": "garasi-ringan", "enabled": True},
    ]}
    d = desired_procs(cfg, "cameras.json")
    assert set(d) == {("live", "taman"), ("garasi",)}      # 2 kamera ringan -> TETAP 1 run_garasi
    assert d[("garasi",)][1] == build_garasi_cmd("cameras.json")


def test_desired_dua_taman():
    cfg = {"kamera": [
        {"nama": "taman", "stream": "taman", "peran": "taman-penuh", "enabled": True},
        {"nama": "belakang", "stream": "belakang", "peran": "taman-penuh", "enabled": True},
    ]}
    d = desired_procs(cfg, "cameras.json")
    assert set(d) == {("live", "taman"), ("live", "belakang")}   # run_live per kamera taman
    assert ("garasi",) not in d


def test_desired_skip_disabled_dan_tanpa_stream():
    cfg = {"kamera": [
        {"nama": "taman", "stream": "taman", "peran": "taman-penuh", "enabled": False},
        {"nama": "garasi", "peran": "garasi-ringan", "enabled": True},   # tanpa stream -> skip
    ]}
    assert desired_procs(cfg, "cameras.json") == {}


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"\n{len(fns)} passed")
    sys.exit(0)
