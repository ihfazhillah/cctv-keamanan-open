"""Test nol-deps untuk logika murni tripwire garasi (seberang-garis berarah oleh
titik-kaki). Geometri (_cross_sign/_segmen_potong) + state Tripwire."""
from run_garasi import Tripwire, _cross_sign, _segmen_potong


# Garis horizontal A->B; sisi NORMAL+ ('maju') = BAWAH layar (y lebih besar).
A, B = (0.2, 0.5), (0.8, 0.5)


def test_cross_sign_sisi():
    assert _cross_sign(A, B, (0.5, 0.6)) > 0      # di bawah garis -> maju
    assert _cross_sign(A, B, (0.5, 0.4)) < 0      # di atas garis -> mundur
    assert abs(_cross_sign(A, B, (0.5, 0.5))) < 1e-9   # persis di garis


def test_segmen_potong_di_dalam_bentang():
    assert _segmen_potong((0.5, 0.4), (0.5, 0.6), A, B) is True    # vertikal lewat tengah


def test_segmen_tak_potong_di_luar_bentang():
    # gerak vertikal di x=0.9 (di luar bentang ruas 0.2..0.8) -> tak memotong RUAS
    assert _segmen_potong((0.9, 0.4), (0.9, 0.6), A, B) is False


def test_segmen_tak_potong_sejajar_sesisi():
    assert _segmen_potong((0.3, 0.4), (0.6, 0.45), A, B) is False  # dua-duanya di atas


def test_lintas_maju():
    tw = Tripwire(A, B)
    assert tw.update(1, (0.5, 0.4), t=0.0) is None      # frame pertama: belum ada lintasan
    assert tw.update(1, (0.5, 0.6), t=1.0) == "maju"    # naik ke sisi bawah = maju


def test_lintas_mundur():
    tw = Tripwire(A, B)
    tw.update(2, (0.5, 0.6), t=0.0)
    assert tw.update(2, (0.5, 0.4), t=1.0) == "mundur"


def test_tak_lintas_bila_sesisi():
    tw = Tripwire(A, B)
    tw.update(3, (0.3, 0.4), t=0.0)
    assert tw.update(3, (0.6, 0.45), t=1.0) is None      # bergerak tapi tak menyeberang


def test_tak_lintas_di_luar_bentang():
    tw = Tripwire(A, B)
    tw.update(4, (0.9, 0.4), t=0.0)
    assert tw.update(4, (0.9, 0.6), t=1.0) is None       # lewat jauh di kanan bentang garis


def test_cooldown_per_track_arah():
    tw = Tripwire(A, B, cooldown_s=8.0)
    tw.update(5, (0.5, 0.4), t=0.0)
    assert tw.update(5, (0.5, 0.6), t=1.0) == "maju"     # lintas pertama
    tw.update(5, (0.5, 0.4), t=2.0)                       # balik (arah mundur, beda key -> boleh)
    assert tw.update(5, (0.5, 0.6), t=3.0) is None       # maju lagi < cooldown -> ditahan
    tw.update(5, (0.5, 0.4), t=10.0)
    assert tw.update(5, (0.5, 0.6), t=12.0) == "maju"    # > cooldown -> lahir lagi


def test_track_terpisah_independen():
    tw = Tripwire(A, B)
    tw.update(1, (0.5, 0.4), t=0.0)
    tw.update(2, (0.5, 0.6), t=0.0)
    assert tw.update(1, (0.5, 0.6), t=1.0) == "maju"
    assert tw.update(2, (0.5, 0.4), t=1.0) == "mundur"


def test_prune_buang_track_basi():
    tw = Tripwire(A, B)
    tw.update(9, (0.5, 0.4), t=0.0)
    assert 9 in tw.last
    tw.prune(t=100.0, ttl=30.0)
    assert 9 not in tw.last                               # lama tak terlihat -> dilupakan


def test_garis_lines_normalisasi():
    from run_garasi import _garis_lines
    satu = {"garis": [[0.1, 0.2], [0.3, 0.4]], "label": {"maju": "a"}}
    assert len(_garis_lines(satu)) == 1                 # dict tunggal (legacy)
    assert len(_garis_lines([satu, satu])) == 2         # list banyak garis
    assert _garis_lines(None) == []
    assert _garis_lines([{"garis": [[0, 0]]}]) == []    # garis tak lengkap -> dibuang
