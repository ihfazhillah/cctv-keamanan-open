"""Test nol-deps utk logika pilih-hapus retensi out/live."""
from retensi_live import pilih_hapus

GB = 1_000_000_000


def _items(spec):
    # spec: list (nama, size_gb, mtime) -> (path, bytes, mtime)
    return [(n, int(g * GB), t) for n, g, t in spec]


def test_di_bawah_cap_tak_hapus():
    it = _items([("a", 1, 1), ("b", 1, 2)])
    assert pilih_hapus(it, cap_bytes=5 * GB, keep=0) == []


def test_hapus_tertua_dulu_sampai_di_bawah_cap():
    # total 4GB, cap 2GB -> buang tertua (mtime kecil) sampai <=2GB
    it = _items([("tua", 1, 1), ("tengah", 1, 2), ("baru", 2, 3)])
    assert pilih_hapus(it, cap_bytes=2 * GB, keep=0) == ["tua", "tengah"]


def test_keep_lindungi_terbaru():
    # total 3GB, cap 0 -> mau habis, tapi keep=1 sisakan yang TERbaru
    it = _items([("t1", 1, 10), ("t2", 1, 20), ("t3", 1, 30)])
    assert pilih_hapus(it, cap_bytes=0, keep=1) == ["t1", "t2"]


def test_keep_ge_jumlah_tak_hapus_apapun():
    it = _items([("a", 5, 1), ("b", 5, 2)])
    assert pilih_hapus(it, cap_bytes=0, keep=5) == []


def test_tie_break_stabil_by_path():
    # mtime sama -> urut by path, deterministik
    it = _items([("b", 1, 1), ("a", 1, 1), ("c", 1, 1)])
    assert pilih_hapus(it, cap_bytes=1 * GB, keep=0) == ["a", "b"]


if __name__ == "__main__":
    import sys
    fn = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fn:
        f()
        print("ok", f.__name__)
    print(f"\n{len(fn)} passed")
    sys.exit(0)
