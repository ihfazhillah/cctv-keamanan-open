"""Test logika murni SceneNotifier (gerbang notif anti-spam okupansi-kontinu).
Debounce masuk (min_presence) + grace panjang (gabung kedipan) = anti masuk/kosong spam."""
from live import SceneNotifier


def kinds(events):
    return [e["kind"] for e in events]


def _masuk(sn, t0=0.0, mp=5.0):
    """Bantu: picu scene_masuk (okupansi bertahan >= min_presence)."""
    out = []
    for k in range(int(mp) + 2):
        out += sn.update(1, t0 + k * 1.0)
    return out


def test_masuk_hanya_setelah_min_presence():
    sn = SceneNotifier(min_presence_s=5.0, grace_s=90.0)
    assert kinds(sn.update(1, 0.0)) == []            # onset -> belum umumkan (debounce)
    assert kinds(sn.update(1, 3.0)) == []            # masih < min_presence
    ev = sn.update(1, 5.0)                            # >= 5s -> scene_masuk sekali
    assert kinds(ev) == ["scene_masuk"] and ev[0]["count"] == 1
    assert kinds(sn.update(1, 6.0)) == []            # tak dobel


def test_lewat_sekejap_senyap_total():
    sn = SceneNotifier(min_presence_s=5.0, grace_s=90.0)
    sn.update(1, 0.0)                                 # kedip 2s lalu hilang
    sn.update(1, 2.0)
    out = []
    for k in range(200):                             # kosong lama -> tutup senyap (tak pernah diumumkan)
        out += sn.update(0, 3.0 + k)
    assert out == []                                 # TAK ada scene_masuk MAUPUN scene_kosong


def test_grace_panjang_gabung_kedipan():
    sn = SceneNotifier(min_presence_s=5.0, grace_s=90.0)
    assert kinds(_masuk(sn, 0.0)) == ["scene_masuk"]
    # kosong 50s (< grace 90) lalu terisi lagi -> TETAP presence yg sama (tak re-arm)
    out = []
    for k in range(50):
        out += sn.update(0, 10.0 + k)
    out += sn.update(1, 65.0)
    out += sn.update(1, 70.0)
    assert out == []                                 # tak ada masuk/kosong baru


def test_kosong_setelah_grace_lalu_rearm():
    sn = SceneNotifier(min_presence_s=5.0, grace_s=90.0)
    _masuk(sn, 0.0)                                   # masuk @~5
    out = []
    for k in range(95):                              # kosong >= grace -> scene_kosong
        out += sn.update(0, 10.0 + k)
    assert kinds(out) == ["scene_kosong"]
    # jauh kemudian terisi lagi -> peristiwa BARU (setelah debounce sendiri)
    assert kinds(_masuk(sn, 300.0)) == ["scene_masuk"]


def test_tambah_saat_count_naik_bertahan():
    sn = SceneNotifier(min_presence_s=0.0, grace_s=90.0, bump_persist_s=3.0)
    sn.update(1, 0.0)                                 # onset
    assert kinds(sn.update(1, 0.1)) == ["scene_masuk"]  # min_presence=0 -> umum langsung, peak=1
    assert sn.update(2, 1.0) == []                    # naik ke 2, belum tahan
    ev = sn.update(2, 4.5)                            # tahan >=3s -> tambah
    assert kinds(ev) == ["scene_tambah"] and ev[0]["count"] == 2 and ev[0]["prev"] == 1


def test_tambah_diredam_bila_spike_sesaat():
    sn = SceneNotifier(min_presence_s=0.0, grace_s=90.0, bump_persist_s=3.0)
    sn.update(1, 0.0); sn.update(1, 0.1)             # masuk (count=1)
    assert sn.update(3, 1.0) == []                    # spike (track pecah) sesaat
    assert sn.update(1, 1.5) == []                    # balik -> kandidat batal
    assert sn.update(1, 2.0) == []                    # tak ada scene_tambah palsu


def test_digest_tiap_interval():
    sn = SceneNotifier(min_presence_s=0.0, grace_s=90.0, digest_s=100.0)
    sn.update(1, 0.0)
    out = sn.update(1, 0.1)                           # scene_masuk @0.1 (last_digest=0.1)
    for k in range(1, 300):
        out += sn.update(1, k * 1.0)
    ds = [e for e in out if e["kind"] == "scene_digest"]
    assert len(ds) == 2                              # @~100 dan @~200


def test_flush_hanya_bila_diumumkan():
    sn = SceneNotifier(min_presence_s=5.0)
    _masuk(sn, 0.0)
    assert kinds(sn.flush(50.0)) == ["scene_kosong"]
    # presence yg belum diumumkan (kedip) -> flush senyap
    sn2 = SceneNotifier(min_presence_s=5.0)
    sn2.update(1, 0.0)
    assert sn2.flush(2.0) == []
