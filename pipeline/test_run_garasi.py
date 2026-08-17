"""Test nol-deps utk logika murni detektor garasi (dalam_jendela + Debounce)."""
import time
from run_garasi import dalam_jendela, Debounce, pilih_kamera, kamera_garasi, _in_window


def _epoch(hh, mm):
    lt = list(time.localtime())
    lt[3], lt[4], lt[5] = hh, mm, 0
    return time.mktime(time.struct_time(lt))


def test_jendela_kosong_selalu_aktif():
    assert dalam_jendela(_epoch(3, 0), []) is True          # fail-safe


def test_jendela_siang():
    j = [{"from": "08:00", "to": "17:00", "aktif": True}]
    assert dalam_jendela(_epoch(12, 0), j) is True
    assert dalam_jendela(_epoch(20, 0), j) is False


def test_jendela_lewat_tengah_malam():
    j = [{"from": "22:00", "to": "06:00", "aktif": True}]
    assert dalam_jendela(_epoch(23, 30), j) is True
    assert dalam_jendela(_epoch(3, 0), j) is True
    assert dalam_jendela(_epoch(12, 0), j) is False


def test_jendela_nonaktif_diabaikan():
    j = [{"from": "22:00", "to": "06:00", "aktif": False}]
    assert dalam_jendela(_epoch(23, 0), j) is False


def test_in_window_batas():
    assert _in_window("08:00", "17:00", 8 * 60) is True     # inklusif awal
    assert _in_window("08:00", "17:00", 17 * 60) is False   # eksklusif akhir


def test_debounce_butuh_streak():
    d = Debounce(need_frames=3, cooldown_s=60)
    assert d.on_frame(True, 0) is False
    assert d.on_frame(True, 1) is False
    assert d.on_frame(True, 2) is True                      # streak ke-3 -> lahir


def test_debounce_reset_saat_kosong():
    d = Debounce(need_frames=3, cooldown_s=60)
    d.on_frame(True, 0); d.on_frame(True, 1)
    assert d.on_frame(False, 2) is False                    # reset streak
    assert d.on_frame(True, 3) is False
    assert d.on_frame(True, 4) is False
    assert d.on_frame(True, 5) is True


def test_debounce_cooldown():
    d = Debounce(need_frames=1, cooldown_s=60)
    assert d.on_frame(True, 100) is True                    # fire pertama
    assert d.on_frame(True, 130) is False                   # < cooldown
    assert d.on_frame(True, 160) is True                    # >= cooldown -> re-fire


def test_pilih_kamera():
    cfg = {"kamera": [
        {"nama": "taman", "peran": "taman-penuh", "enabled": True},
        {"nama": "garasi", "peran": "garasi-ringan", "enabled": True},
    ]}
    assert pilih_kamera(cfg)["nama"] == "garasi"
    cfg["kamera"][1]["enabled"] = False
    assert pilih_kamera(cfg) is None


def test_kamera_garasi_multi():
    cfg = {"kamera": [
        {"nama": "taman", "peran": "taman-penuh", "enabled": True},
        {"nama": "garasi", "peran": "garasi-ringan", "enabled": True},
        {"nama": "gudang", "peran": "garasi-ringan", "enabled": True},
        {"nama": "off1", "peran": "garasi-ringan", "enabled": False},
    ]}
    assert kamera_garasi(cfg) == ["garasi", "gudang"]        # hanya garasi-ringan enabled
    assert kamera_garasi({"kamera": []}) == []


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"\n{len(fns)} passed")
    sys.exit(0)
