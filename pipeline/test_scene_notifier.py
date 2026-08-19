"""Test logika murni SceneNotifier (gerbang notif anti-spam okupansi-kontinu)."""
from live import SceneNotifier


def kinds(events):
    return [e["kind"] for e in events]


def test_masuk_saat_pertama_terisi():
    sn = SceneNotifier()
    assert kinds(sn.update(0, 0.0)) == []
    ev = sn.update(1, 1.0)
    assert kinds(ev) == ["scene_masuk"] and ev[0]["count"] == 1


def test_diam_selama_terisi_kontinu():
    sn = SceneNotifier(digest_s=1800)
    sn.update(1, 0.0)                                 # masuk
    # gerak-gerak/count sama selama 5 menit -> TAK ada notif tambahan
    out = []
    for k in range(1, 300):
        out += sn.update(1, k * 1.0)
    assert out == []                                  # inilah anti-spam-nya


def test_kosong_setelah_grace_lalu_rearm():
    sn = SceneNotifier(grace_s=6.0)
    sn.update(1, 0.0)                                 # masuk
    assert sn.update(0, 3.0) == []                    # kosong < grace -> belum tutup
    ev = sn.update(0, 7.0)                            # kosong >= grace -> tutup
    assert kinds(ev) == ["scene_kosong"]
    assert kinds(sn.update(1, 20.0)) == ["scene_masuk"]  # terisi lagi -> peristiwa BARU


def test_kedip_kosong_singkat_tak_menutup():
    sn = SceneNotifier(grace_s=6.0)
    sn.update(1, 0.0)
    assert sn.update(0, 2.0) == []                    # kedip
    assert kinds(sn.update(1, 3.0)) == []             # terisi lagi -> tetap presence yg sama
    assert sn.update(0, 30.0)[0]["kind"] == "scene_kosong"  # baru tutup setelah grace penuh


def test_tambah_saat_count_naik_bertahan():
    sn = SceneNotifier(bump_persist_s=3.0)
    sn.update(1, 0.0)                                 # masuk count=1
    assert sn.update(2, 1.0) == []                    # naik ke 2, belum tahan
    assert sn.update(2, 2.0) == []
    ev = sn.update(2, 4.5)                            # tahan >=3s -> tambah
    assert kinds(ev) == ["scene_tambah"] and ev[0]["count"] == 2 and ev[0]["prev"] == 1


def test_tambah_diredam_bila_spike_sesaat():
    sn = SceneNotifier(bump_persist_s=3.0)
    sn.update(1, 0.0)
    assert sn.update(3, 1.0) == []                    # spike (track pecah) sesaat
    assert sn.update(1, 1.5) == []                    # balik -> kandidat batal
    assert sn.update(1, 2.0) == []                    # tak ada scene_tambah palsu


def test_digest_tiap_interval():
    sn = SceneNotifier(digest_s=100.0)
    sn.update(1, 0.0)                                 # masuk @0
    out = []
    for k in range(1, 250):
        out += sn.update(1, k * 1.0)
    ds = [e for e in out if e["kind"] == "scene_digest"]
    assert len(ds) == 2                              # @100 dan @200
    assert ds[0]["dur"] == 100.0


def test_flush_menutup_presence_terbuka():
    sn = SceneNotifier()
    sn.update(1, 0.0)
    ev = sn.flush(50.0)
    assert kinds(ev) == ["scene_kosong"]
    assert sn.flush(60.0) == []                      # sudah tertutup
