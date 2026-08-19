"""Logika RULE murni — NOL dependency (stdlib saja).

Portable: `import rules` di repo, di notebook, atau **copy-paste satu file ini ke
Google Colab**. Tak butuh GPU/model/service — cuma `trace` (list observasi per-frame,
JSON) → event. Perception (YOLO → trace) terpisah (lihat harness.py); ini hilir seam.

Isi:
  - geometri seberang-garis: _cross_sign, _segmen_potong
  - Tripwire        : penyeberangan garis berarah oleh titik-kaki
  - FootTracker     : ID titik-kaki ringan (ganti BoT-SORT utk objek cepat)
  - SceneNotifier   : gerbang notif anti-spam okupansi-kontinu
  - _garis_lines/_garis_sig : normalisasi config garis_periksa
  - replay_tripwire / replay_scene : jalankan rule di atas satu `trace`

Bentuk `trace` (satu baris = satu frame):
  {"t": float, "wh":[w,h], "count": int,
   "dets":[{"tid":int|None, "xyxy":[..fraksi..], "kaki":[fx,fy], "conf":.., "zona":..}]}
"""
import json

LUAR = frozenset({"jalan-utama"})   # zona 'luar' (depth 0) — TAK dihitung okupansi dalam (scene count)


# ══ Geometri: seberang RUAS garis berarah ═══════════════════════════════════════
def _cross_sign(a, b, p):
    """Sisi titik p relatif garis BERARAH A->B (cross-product z).
    >0 = sisi NORMAL+ (rotasi +90° dari A->B, kami sebut 'maju'); <0 = seberang; 0 = persis di garis."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _segmen_potong(p, q, a, b):
    """Ruas gerak kaki p->q memotong RUAS garis a->b? (uji orientasi standar). Pakai
    ruas—bukan garis tak-hingga—agar orang yang lewat di luar bentang garis tak terhitung."""
    d1 = _cross_sign(p, q, a)
    d2 = _cross_sign(p, q, b)
    d3 = _cross_sign(a, b, p)
    d4 = _cross_sign(a, b, q)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


class Tripwire:
    """Deteksi penyeberangan garis berarah oleh TITIK-KAKI (BOTTOM_CENTER) per track.
    Simpan posisi kaki terakhir tiap track; saat ruas gerak kaki memotong ruas garis,
    lahirkan arah 'maju' (ke sisi normal+) / 'mundur' (sebaliknya). Murni-state -> mudah
    diuji. Cooldown per (track,arah) meredam getar bolak-balik di ambang garis. Titik
    dalam FRAKSI [0..1] (bebas-resolusi; dipetakan dari box saat runtime, seperti abaikan)."""

    def __init__(self, a, b, cooldown_s=8.0):
        self.a, self.b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
        self.cooldown = cooldown_s
        self.last = {}     # tid -> (kaki, t)  posisi kaki terakhir
        self.fired = {}    # (tid, arah) -> t terakhir lahir

    def update(self, tid, kaki, t):
        """Return 'maju'/'mundur' bila kaki `tid` BARU menyeberang garis, else None."""
        prev = self.last.get(tid)
        self.last[tid] = (kaki, t)
        if prev is None:
            return None
        if not _segmen_potong(prev[0], kaki, self.a, self.b):
            return None
        arah = "maju" if _cross_sign(self.a, self.b, kaki) > 0 else "mundur"
        key = (tid, arah)
        if t - self.fired.get(key, -1e18) < self.cooldown:
            return None
        self.fired[key] = t
        return arah

    def prune(self, t, ttl=30.0):
        """Lupakan track yg lama tak terlihat -> dict tak membengkak; ID baru mulai bersih."""
        for tid in [k for k, (_, tt) in list(self.last.items()) if t - tt > ttl]:
            del self.last[tid]


class FootTracker:
    """Tracker titik-kaki RINGAN (nearest-neighbor + gate jarak) — pengganti BoT-SORT
    khusus garasi. Bukti (pelari 03:10): BoT-SORT gagal beri ID stabil (tid None,
    flicker) & conf jatuh <0.35 TEPAT saat menyeberang -> crossing luput. NN over
    frame RAPAT + conf rendah + toleransi lubang (ttl) menangkapnya: satu objek cepat
    = asosiasi trivial & andal, tanpa jeda-konfirmasi BoT-SORT. Murni-state, teruji.
    Titik fraksi [0..1]."""

    def __init__(self, max_dist=0.18, ttl=1.5):
        self.max_dist = max_dist       # jarak maks asosiasi antar-frame (fraksi)
        self.ttl = ttl                 # detik: track tak-terlihat > ttl -> dilupakan (jembatani lubang deteksi)
        self.tracks = {}               # id -> (kaki, t)
        self._next = 1

    def update(self, feet, t):
        """feet: list (fx,fy). Return list (id,(fx,fy)) — ID stabil via NN greedy."""
        for i in [k for k, (_, tt) in list(self.tracks.items()) if t - tt > self.ttl]:
            del self.tracks[i]
        out, used = [], set()
        for f in feet:                 # tiap deteksi -> track terdekat dalam gate
            best, bd = None, self.max_dist
            for i, (pf, _) in self.tracks.items():
                if i in used:
                    continue
                d = ((f[0] - pf[0]) ** 2 + (f[1] - pf[1]) ** 2) ** 0.5
                if d < bd:
                    bd, best = d, i
            if best is None:
                best = self._next
                self._next += 1
            used.add(best)
            self.tracks[best] = (f, t)
            out.append((best, f))
        return out


def _garis_lines(gp):
    """Normalisasi garis_periksa -> list of dict {garis,label,nama}. Terima BENTUK:
    dict tunggal (legacy 1 garis) ATAU list (banyak garis). Buang entri tak valid."""
    if isinstance(gp, dict):
        gp = [gp]
    out = []
    if isinstance(gp, list):
        for g in gp:
            if isinstance(g, dict) and isinstance(g.get("garis"), list) and len(g["garis"]) == 2:
                out.append(g)
    return out


def _garis_sig(gp):
    """Tanda-tangan ringkas garis_periksa utk deteksi perubahan (live-reload tripwire)."""
    return json.dumps(gp, sort_keys=True) if gp is not None else ""


class SceneNotifier:
    """Gerbang notif anti-spam berbasis OKUPANSI KONTINU (bukan identitas/ReID).
    Masalah: orang loiter berjam-jam -> episode/loiter fire berulang tiap gerakan
    kecil = spam. Solusi: satu presence KONTINU = satu peristiwa. `count` = jumlah
    orang SERENTAK di dalam (dihitung pemanggil dari tid_to_zone, exclude jalan-utama).

    State machine (murni-logika & event-based, teruji test_scene_notifier):

        KOSONG ──(count>0)──────────────────> TERISI     -> scene_masuk (arm)
        TERISI ──(count > puncak, tahan bump_persist_s)   -> scene_tambah (proxy 'orang
                                                             baru' tanpa ReID)
        TERISI ──(tiap digest_s)────────────> scene_digest (ringkasan 'masih ada orang')
        TERISI ──(count==0 selama ≥ grace_s)─> KOSONG     -> scene_kosong (re-arm)

    Event: {"kind": "scene_masuk|scene_tambah|scene_digest|scene_kosong", "at", ...}.
    """

    def __init__(self, grace_s=90.0, digest_s=1800.0, bump_persist_s=3.0, min_presence_s=5.0):
        self.grace_s = grace_s              # kosong selama ini -> tutup presence (re-arm). PANJANG:
                                            # gabung kedipan okupansi jadi SATU presence (anti masuk/kosong spam)
        self.digest_s = digest_s            # interval ringkasan 'masih ada orang'
        self.bump_persist_s = bump_persist_s  # kenaikan count harus bertahan -> redam track pecah
        self.min_presence_s = min_presence_s  # okupansi harus bertahan segini SEBELUM 'scene_masuk' (buang lewat/kedip)
        self._reset()

    def _reset(self):
        self.active = False                 # ada okupansi (mungkin belum diumumkan)
        self.announced = False              # 'scene_masuk' sudah dikirim? (lewat debounce min_presence)
        self.start_t = None                 # onset okupansi (utk dur & debounce)
        self.last_seen_t = None             # kapan terakhir count>0
        self.peak = 0
        self.last_digest_t = None
        self._cand = 0                      # kandidat count-naik yg sedang 'diuji tahan'
        self._cand_since = None

    def update(self, count, t):
        """count = jumlah orang serentak di dalam (int ≥0). -> list event notif."""
        if not self.active:
            if count > 0:                   # okupansi MULAI -> tunggu min_presence dulu (belum umumkan)
                self.active = True
                self.announced = False
                self.start_t = self.last_seen_t = t
                self.peak = count
                self._cand, self._cand_since = 0, None
            return []

        out = []
        if count > 0:
            self.last_seen_t = t
            if not self.announced:
                if t - self.start_t >= self.min_presence_s:   # bertahan cukup -> UMUMKAN sekali
                    self.announced = True
                    self.last_digest_t = t
                    self.peak = count
                    out.append({"kind": "scene_masuk", "at": t, "count": count})
                return out                                    # masih debounce / baru umum -> stop di sini
            if count > self.peak:                             # kenaikan -> uji tahan bump_persist_s
                if count != self._cand:
                    self._cand, self._cand_since = count, t
                elif t - self._cand_since >= self.bump_persist_s:
                    out.append({"kind": "scene_tambah", "at": t, "count": count, "prev": self.peak})
                    self.peak = count
                    self._cand, self._cand_since = 0, None
            else:
                self._cand, self._cand_since = 0, None
            if t - self.last_digest_t >= self.digest_s:
                out.append({"kind": "scene_digest", "at": t, "dur": t - self.start_t,
                            "count": count, "peak": self.peak})
                self.last_digest_t = t
        elif t - self.last_seen_t >= self.grace_s:            # kosong cukup lama -> tutup
            if self.announced:                                # kalau tak pernah diumumkan (lewat sekejap) -> senyap
                out.append(self._kosong(t))
            self._reset()
        return out

    def flush(self, t):
        """Tutup presence terbuka saat shutdown (hanya bila sudah diumumkan)."""
        announced = self.active and self.announced
        out = [self._kosong(t)] if announced else []
        self._reset()
        return out

    def _kosong(self, t):
        return {"kind": "scene_kosong", "at": t, "start": self.start_t,
                "dur": (self.last_seen_t or t) - self.start_t, "peak": self.peak}


# ══ REPLAY: trace -> event (pakai kelas di atas) ════════════════════════════════
def replay_tripwire(tr, lines, use_foottracker=True):
    """Trace -> penyeberangan garis. lines = format garis_periksa (dict/list
    {nama,garis,label}). tid dari FootTracker (default; andal utk objek cepat) atau
    dari trace bila sudah ada (tracker=botsort saat trace)."""
    tws = []
    for i, g in enumerate(_garis_lines(lines)):
        tws.append((Tripwire(g["garis"][0], g["garis"][1]),
                    g.get("nama") or f"garis{i + 1}", g.get("label") or {}))
    ft = FootTracker() if use_foottracker else None
    out = []
    for f in tr:
        if ft is not None:
            pairs = ft.update([tuple(d["kaki"]) for d in f["dets"]], f["t"])
        else:
            pairs = [(d["tid"], tuple(d["kaki"])) for d in f["dets"] if d["tid"] is not None]
        for tid, kaki in pairs:
            for tw, nama, lab in tws:
                a = tw.update(tid, kaki, f["t"])
                if a:
                    out.append({"kind": "crossing", "garis": nama, "arah": lab.get(a, a),
                                "at": f["t"], "tid": tid})
        for tw, _, _ in tws:
            tw.prune(f["t"])
    return out


def replay_scene(tr, notifier=None, **kw):
    """Trace (butuh `count` -> trace dgn zone_file) -> event SceneNotifier. Beri
    `notifier=SceneNotifier(...)` atau kwargs (grace_s=, min_presence_s=, digest_s=)."""
    sn = notifier or SceneNotifier(**kw)
    out = []
    for f in tr:
        out += sn.update(f["count"], f["t"])
    if tr:
        out += sn.flush(tr[-1]["t"] + 1e-3)
    return out
