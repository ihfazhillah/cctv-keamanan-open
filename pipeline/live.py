from collections import deque, Counter
import queue
import threading
import time
import uuid
from rules import SceneNotifier, LUAR  # noqa: F401 (pindah ke rules.py; re-export)


class PendingNotifier:
    def __init__(self, pre, post):
        self.pre = pre
        self.post = post
        self.registry = {}

    def add(self, trigger, pre_frames): 
        key = str(uuid.uuid4())
        self.registry[key] = {
            "frames": list(pre_frames),
            "t0": self._resolve_t0(trigger),
            "t1": self._resolve_t1(trigger),
            "trigger": trigger
        }
    
    def feed(self, t, frame):
        for value in self.registry.values():
            if value["t1"] >= t and value["t0"] < t:
                value["frames"].append(frame)

    def due(self, t): # pending dengan t1 <= t, DAN keluarkan dari registry
        pending = []

        for key, value in self.registry.copy().items():
            if value["t1"] <= t:
                pending.append({
                    "t1": value["t1"],
                    "t0": value["t0"],
                    "trigger": value["trigger"],
                    "frames": value["frames"]
                })

                del self.registry[key]

        return pending

    def _resolve_t0(self, trigger):
        match trigger["kind"]:
            case "loiter":
                # klip loiter = SELURUH masa berdiam [start, at+post], bukan cuma
                # sekitar ambang. Butuh buffer >= loiter_s (lihat ClipBuffer.keep_s).
                return trigger["start"]
            case "close" | "keluar" | "masuk":     # kejadian berdurasi [start,end]
                return trigger["start"] - self.pre
            case _:                                 # passage (@at) -> jendela pendek
                return trigger["at"] - self.pre

    def _resolve_t1(self, trigger):
        match trigger["kind"]:
            case "loiter":
                return trigger["at"] + self.post
            case "close" | "keluar" | "masuk":
                return trigger["end"] + self.post
            case _:
                return trigger["at"]


class FrameBuffer:
    def __init__(self, keep_s):
        maxlen = keep_s * 30 * 1.2 # fps = 30. It's more than our nvr can give
        self.buffer = deque(maxlen=int(maxlen)) # truncate kasar, pembulatan tidak penting. 30 > dari nvr yang kita miliki
        self.keep_s = keep_s
        self.lock = threading.Lock()

    def add(self, timestamp, frame):
        self.lock.acquire()

        self.buffer.append((timestamp, frame))
        while self.buffer and self.buffer[0][0] < timestamp - self.keep_s:
            self.buffer.popleft()

        self.lock.release()

    def get(self, t0, t1):
        self.lock.acquire()

        frames = [b[1] for b in self.buffer if b[0] >= t0 and b[0] <= t1]

        self.lock.release()

        return frames





def read_next(cap):
    ok, frame = cap.read()
    if not ok:
        return
    return time.time(), frame




class EpisodeTracker:
    def __init__(self, exit_hysteresis=0.0, loiter_s=None, enter_inertia=0.0):
        self.last_episode = {}
        self.exit_hysteresis = exit_hysteresis
        if loiter_s is not None and loiter_s <= enter_inertia:
            raise ValueError(f"loiter_s aktif tidak boleh <= enter_inertia. Dapat {loiter_s=} {enter_inertia=}")

        self.loiter_s = loiter_s # None -> disabled
        self.enter_inertia = enter_inertia


    def update(self, occupied_set, timestamp):
        """
        occupied_set -> set nama tempat. Misal: jalan, taman.
        """


        triggers = []

        if occupied_set:
            for zone in occupied_set:
                if zone in self.last_episode:
                    triggers += self.loiter(zone, timestamp)
                    self.perpanjang(zone, timestamp)
                    
                else:
                    # buka episode
                    self.draft(zone, timestamp)


        for zone, time_range in self.last_episode.copy().items():
            if zone not in occupied_set:
                if (timestamp - time_range["end"]) >= self.exit_hysteresis:
                    tutup_trigger = self.tutup(zone)
                    if tutup_trigger:
                        triggers.append(tutup_trigger)

                if time_range["draft"]:
                    self.drop(zone)


        return triggers

    def flush(self):
        triggers = []
        for zone in self.last_episode.copy():
            trigger = self.tutup(zone)
            if trigger:
                triggers.append(trigger)
        return triggers


    def tutup(self, zone):
        if zone in self.last_episode and self.last_episode[zone]["draft"]:
            return 

        trigger = {"kind": "close", "zone": zone, "start": self.last_episode[zone]["start"], "end": self.last_episode[zone]["end"]}
        del self.last_episode[zone]
        return trigger 

    def drop(self, zone):
        del self.last_episode[zone]

    def loiter(self, zone, timestamp):
        triggers = []
        if self.loiter_s is not None:

            already_alerted = self.last_episode[zone]["alerted"]
            can_loiter = (timestamp - self.last_episode[zone]["start"]) >= self.loiter_s

            if can_loiter and not already_alerted:

                start = self.last_episode[zone]["start"]
                self.last_episode[zone]["alerted"] = True
                triggers.append({"kind": "loiter", "zone": zone, "start": start, "at": start + self.loiter_s})
        return triggers

    def perpanjang(self, zone, timestamp):
        if timestamp - self.last_episode[zone]["start"] >= self.enter_inertia:
            self.last_episode[zone]["draft"] = False

        self.last_episode[zone]["end"] = timestamp

    def draft(self, zone, timestamp):
        self.last_episode[zone] = {"start": timestamp, "end": timestamp, "alerted": False, "draft": self.enter_inertia > 0} 




class PassageTracker:
    GATES = [
            # update add pinte as gate, karena terkadang dapat
        {"zones": {"teras", "pintu"}, "lahir": "KELUAR rumah", "mati": "MASUK rumah"},
        {"zones": {"jalan-masuk", "tangga"}, "lahir" : "MASUK property", "mati": "KELUAR property"}
    ]

    def __init__(self, ambang_s, ignore=frozenset({"jalan-utama"}), min_presence_s=0.0):
        self.ambang_s = ambang_s
        self.ignore = ignore
        self.min_presence_s = min_presence_s

        # state
        self.presence = None # or {start, end, first, last}

    def update(self, occupied, timestamp):
        zones = occupied - self.ignore

        # update passages
        passages = []
        if self.is_kadaluarsa(timestamp):
            passages = self.tutup()

        # update state
        if zones:
            if self.presence:
                self.perpanjang(zones, timestamp)
            else:
                self.buka(zones, timestamp)

        return passages
    
    def tutup(self):
        passages = []

        if self.presence:
            pass_min_presence = (self.presence["end"] - self.presence["start"]) >= self.min_presence_s

            if pass_min_presence:
                for gate in self.GATES:
                    if self.presence["first"] & gate["zones"]:
                        passages.append({"kind": gate["lahir"], "at": self.presence["start"]})
                    if self.presence["last"] & gate["zones"]:
                        passages.append({"kind": gate["mati"], "at": self.presence["end"]})

        self.presence = None

        passages.sort(key=lambda p: p["at"])
        return passages

    def buka(self, zones, timestamp):
        self.presence = {
            "start": timestamp,
            "end": timestamp,
            "first": zones,
            "last": zones
        }

    def perpanjang(self, zones, timestamp):
        self.presence.update({"end": timestamp, "last": zones})

    def is_kadaluarsa(self, timestamp):
        return self.presence and timestamp - self.presence["end"] >= self.ambang_s



# ── Sumbu KEDALAMAN zona (SATU sumber kebenaran) ──────────────────────────────
# luar(0) -> tepi properti(1) -> halaman(2) -> pintu/ambang rumah(3). Interior di
# balik pintu TAK teramati. Dipakai bersama TrackPassageTracker (arah per-track) &
# SceneEpisode (arah episode) -> jangan gandakan; edit di sini saja bila zona berubah.
ZONE_DEPTH = {
    "jalan-masuk": 1, "tangga": 1,              # tepi properti
    "taman": 2, "dekat-kolam": 2, "teras": 2,   # halaman/beranda (teras MASIH halaman)
    "pintu": 3,                                 # ambang rumah
}
                                                # LUAR -> rules.py (di-import di atas; re-export)


def depth_of(zone, ignore=LUAR):
    """Kedalaman satu zona. None / diabaikan / tak dikenal -> 0 (luar)."""
    if zone is None or zone in ignore:
        return 0
    return ZONE_DEPTH.get(zone, 0)


class TrackPassageTracker:
    """Track-sentris: arah masuk/keluar diturunkan dari perpindahan DEPTH tiap
    `track_id` sepanjang sumbu luar->dalam — bukan tebakan first/last zona pada
    presence gabungan (lihat PassageTracker zone-centric di atas).

    Menjawab langsung batas sadar yang dipaku test_passages.py: "gate di tengah
    presence tenggelam ... obatnya person/track-centric" (P10) dan "orang di teras
    bisa sedang keluar ATAU masuk" (P1/docstring). Karena kini per-badan:
      - gate di tengah lintasan TIDAK tenggelam (perpindahan teramati memancarkan),
      - dua orang di dua zona TIDAK saling mengacau (state per track_id),
      - satu badan diam TIDAK memancarkan KELUAR+MASUK sekaligus.

    Sumbu DEPTH (khusus geometri kamera 102; edit di satu tempat bila zona berubah):
        0 = luar semua zona (jalan-utama / None) — jalanan, TAK teramati kamera
        1 = jalan-masuk, tangga   (tepi properti)
        2 = taman, dekat-kolam, teras   (halaman/beranda, di dalam properti)
        3 = pintu                 (ambang rumah; interior di baliknya TAK teramati)

    Aturan lintas-batas per track — DUA batas ke wilayah tak-teramati, pola SAMA
    (lahir/mati di batas), orientasi terbalik:
      BATAS PROPERTI (tepi, depth 1) — sisi-luar (jalanan) tak teramati:
          lahir di depth 1              -> MASUK property @ saat muncul
          mati (>=ambang_s) di depth 1  -> KELUAR property @ saat terakhir terlihat
      BATAS RUMAH (pintu, depth 3) — interior tak teramati. Arah dari LINTASAN track
        relatif pintu (bukan transisi/lahir-mati mentah -> tahan kaki-jitter & anak lari):
          datang dari halaman (pernah depth 1/2 SEBELUM pintu) lalu lenyap di pintu
            -> MASUK rumah @ terakhir terlihat
          muncul di pintu lalu ke halaman (depth 1/2 SESUDAH pintu)
            -> KELUAR rumah @ saat muncul
          halaman->pintu->halaman (lewat/menjejak lalu balik) atau lahir&mati di pintu
            tanpa ke halaman (ambigu) -> TAK ada event (jaga presisi).
        Butuh `pintu` MENANG di tumpang-tindih dgn teras (lihat muat_zone/Occupancy),
        sebab kaki di ambang sering jatuh di irisan kedua poligon.

    Debounce: depth turun ke 0 (di luar zona / track hilang) hanya diakui setelah
    ambang_s — kedip None sesaat tidak menutup. min_presence_s: buang seluruh event
    sebuah track bila umurnya < ambang itu (blip phantom), jika tidak rilis begitu lewat.
    """

    ZONE_DEPTH = ZONE_DEPTH   # pakai sumber modul-level (jangan gandakan)
    EDGE_DEPTH = 1    # tepi properti: lahir=MASUK property, mati=KELUAR property (luar tak teramati)
    HOUSE_DEPTH = 3   # ambang rumah: lahir=KELUAR rumah, mati=MASUK rumah (interior tak teramati)

    def __init__(self, ambang_s=3.0, min_presence_s=0.0, ignore=frozenset({"jalan-utama"})):
        self.ambang_s = ambang_s
        self.min_presence_s = min_presence_s
        self.ignore = ignore
        self.tracks = {}   # tid -> {depth, seen_t (terakhir depth>=1), start_t, pending:[ev]}
        self.last_t = 0.0

    def _depth(self, zone):
        if zone is None or zone in self.ignore:
            return 0
        return self.ZONE_DEPTH.get(zone, 0)

    def _emit(self, st, ev):
        st["pending"].append(ev)

    def _presence(self, st):
        return st["seen_t"] - st["start_t"]   # rentang kehadiran teramati (bukan s.d. finalisasi)

    def _release(self, st):
        """Rilis event yang sudah lolos min_presence; sisanya tetap ditahan."""
        if not st["pending"] or self._presence(st) < self.min_presence_s:
            return []
        out = st["pending"]
        st["pending"] = []
        return out

    def _finalize(self, tid, t):
        st = self.tracks.pop(tid)
        if self._presence(st) < self.min_presence_s:
            return []                       # blip phantom -> buang semua event track ini
        out = list(st["pending"])           # event tertahan yang sudah "sah"
        # arah RUMAH dari LINTASAN relatif pintu (bukan lahir/mati mentah): tahan thd
        # kaki-jitter di batas & kekeliruan arah saat orang pertama terlihat di ambang.
        if st["seen_pintu"]:
            if st["pre_yard"] and not st["post_yard"]:    # datang dari halaman lalu lenyap di pintu
                out.append({"kind": "MASUK rumah", "at": st["seen_t"]})
            elif st["post_yard"] and not st["pre_yard"]:  # muncul di pintu lalu ke halaman
                out.append({"kind": "KELUAR rumah", "at": st["start_t"]})
            # pre&post (halaman->pintu->halaman = lewat) / keduanya nihil (ambigu) -> diam
        if st["depth"] == self.EDGE_DEPTH:  # hilang di tepi -> pergi ke jalanan
            out.append({"kind": "KELUAR property", "at": st["seen_t"]})
        return out

    def update(self, track_zones, t):
        # track_zones: {track_id: nama_zona | None} untuk FRAME INI (person saja)
        self.last_t = t
        out = []

        # 1) finalisasi track yang sudah lewat ambang tanpa berada di zona nyata
        for tid in list(self.tracks):
            in_frame_grounded = track_zones.get(tid) is not None and self._depth(track_zones.get(tid)) >= 1
            if not in_frame_grounded and (t - self.tracks[tid]["seen_t"]) >= self.ambang_s:
                out += self._finalize(tid, t)

        # 2) proses track di frame ini
        for tid, zone in track_zones.items():
            cur = self._depth(zone)

            if tid not in self.tracks:
                if cur == 0:
                    continue                # muncul di luar zona -> belum ada track "grounded"
                st = {"depth": cur, "seen_t": t, "start_t": t, "pending": [],
                      "seen_pintu": cur == self.HOUSE_DEPTH,   # lahir tepat di ambang pintu
                      "pre_yard": cur in (1, 2),               # lahir di halaman/tepi (sblm pintu)
                      "post_yard": False}
                self.tracks[tid] = st
                if cur == self.EDGE_DEPTH:
                    self._emit(st, {"kind": "MASUK property", "at": t})
                # arah RUMAH ditentukan saat finalisasi dari lintasan (lihat _finalize)
            else:
                st = self.tracks[tid]
                if cur >= 1:                # hanya zona nyata yang memperbarui posisi
                    if cur == self.HOUSE_DEPTH:
                        st["seen_pintu"] = True
                    elif cur in (1, 2):     # halaman/tepi: sebelum vs sesudah pintu?
                        if st["seen_pintu"]:
                            st["post_yard"] = True
                        else:
                            st["pre_yard"] = True
                    st["depth"] = cur
                    st["seen_t"] = t
                # cur==0 (kedip None): tahan, biarkan grace ambang_s memutuskan

            out += self._release(self.tracks[tid])

        return out

    def flush(self):
        out = []
        for tid in list(self.tracks):
            out += self._finalize(tid, self.last_t)
        return out


class RuleEngine:
    def __init__(
        self,
        exit_hysteresis=0.0,
        loiter_s=None,
        ambang_s=3.0,
        ignore=frozenset({"jalan-utama"}),
        enter_inertia=0.0,
        min_presence_s=0.0
    ):

        self.ep_tracker = EpisodeTracker(exit_hysteresis, loiter_s, enter_inertia=enter_inertia)
        self.passage_tracker = TrackPassageTracker(ambang_s, min_presence_s, ignore)
        self.last_ts = 0.0


    def update(self, occupied, track_zones, t):
        if t < self.last_ts:
            return []

        episodes = self.ep_tracker.update(occupied, t)
        passages = self.passage_tracker.update(track_zones, t)

        self.last_ts = t

        return episodes + passages

    def flush(self):
        episodes = self.ep_tracker.flush()
        passages = self.passage_tracker.flush()
        return episodes + passages


class TransitAggregator:
    """Gabungkan passage SE-ARAH yang berdekatan jadi SATU transit, lalu pancarkan
    satu trigger klip video membentang transit itu. Menjawab: "terdeteksi keluar/
    masuk tapi tak ada video" -- passage lewat-cepat dulu cuma snapshot; kini satu
    keluar (KELUAR rumah + KELUAR property) = satu video, tanpa spam per-passage.

    - feed(trigger): serap passage; kalau se-arah & dalam join_gap dgn transit
      terbuka -> perpanjang; kalau tidak -> transit lama siap dipancarkan, buka baru.
    - due(t): pancarkan transit yang sudah HENING >= emit_delay (biar post-roll
      sempat masuk buffer sebelum diklip). emit_delay harus >= post.
    Trigger transit: {kind:'keluar'|'masuk', start, end, gates:[...], at:start}."""

    DIR = {"KELUAR rumah": "keluar", "KELUAR property": "keluar",
           "MASUK rumah": "masuk", "MASUK property": "masuk"}

    def __init__(self, emit_delay=6.0, join_gap=10.0):
        self.emit_delay = emit_delay
        self.join_gap = join_gap
        self.open = None        # {dir, start, end, gates:set}
        self.ready = []         # transit selesai, menunggu due()

    def feed(self, trigger):
        d = self.DIR.get(trigger["kind"])
        if d is None:
            return
        at = trigger["at"]
        gate = trigger["kind"].split(" ", 1)[1]     # 'rumah' / 'property'
        if self.open and self.open["dir"] == d and (at - self.open["end"]) <= self.join_gap:
            self.open["end"] = at
            self.open["gates"].add(gate)
        else:
            if self.open:
                self.ready.append(self.open)        # arah beda / jeda besar -> transit lama selesai
            self.open = {"dir": d, "start": at, "end": at, "gates": {gate}}

    def due(self, t):
        out = self.ready
        self.ready = []
        if self.open and (t - self.open["end"]) >= self.emit_delay:
            out.append(self.open)
            self.open = None
        return [self._trigger(x) for x in out]

    def flush(self):
        out = self.ready + ([self.open] if self.open else [])
        self.ready = []
        self.open = None
        return [self._trigger(x) for x in out]

    def _trigger(self, x):
        return {"kind": x["dir"], "start": x["start"], "end": x["end"],
                "gates": sorted(x["gates"]), "at": x["start"]}


class SceneEpisode:
    """Bingkai EPISODE level-scene dari OCCUPANCY zona — BUKAN track_id, jadi kebal
    kedip/pecah ID (kelemahan yang kita temukan pada anak berlari).

    State machine (tak peduli identitas / jumlah orang):

        IDLE ──(ada zona terisi)──────────────> ACTIVE
        ACTIVE ──(semua kosong ≥ grace_s)─────> IDLE            (tutup episode)
        ACTIVE ──(durasi ≥ max_s)─────────────> tutup, lalu buka lagi (potong panjang)

    Murni-logika & EVENT-BASED: update() tak menyentuh frame/berkas; ia hanya
    memancarkan event untuk dikonsumsi perekam I/O:
        {"kind": "episode_mulai", "at": t}
        {"kind": "episode", "start", "end", "arah", "gates", "zona"}

    `gates` = zona sesuai URUTAN kemunculan; `arah` (masuk/keluar/lewat) dari tren
    kedalaman (zona pertama vs terakhir muncul) — kasar tapi kebal ID. Event masuk/
    keluar yang presisi tetap dari TrackPassageTracker (ditaruh sbg penanda di dalam).
    """

    def __init__(self, grace_s=6.0, max_s=300.0):
        self.grace_s = grace_s      # sepi selama ini -> episode ditutup
        self.max_s = max_s          # potong episode yang kelewat panjang (loiter)
        self._reset()

    def _reset(self):
        self.active = False
        self.start_t = None
        self.last_active_t = None    # kapan terakhir ADA zona terisi
        self.order = []              # zona sesuai urutan kemunculan pertama
        self._seen = set()
        self.count = Counter()       # frame-per-zona (utk deteksi 'dominan di luar')

    def update(self, occupied, t):
        """occupied = set nama zona yang berisi orang di frame ini. -> list event."""
        ada = bool(occupied)
        if not self.active:
            return self._buka(occupied, t) if ada else []

        if ada:
            self.last_active_t = t
            self._catat(occupied)

        if t - self.start_t >= self.max_s:               # kelewat panjang -> potong
            out = [self._ringkas(self.last_active_t)]
            self._reset()
            if ada:
                out += self._buka(occupied, t)           # sambung episode baru
            return out

        if not ada and (t - self.last_active_t) >= self.grace_s:   # sepi cukup lama -> tutup
            out = [self._ringkas(self.last_active_t)]
            self._reset()
            return out

        return []

    def flush(self, t):
        """Tutup episode terbuka saat shutdown (klip terakhir tak tersangkut)."""
        if not self.active:
            return []
        out = [self._ringkas(self.last_active_t)]
        self._reset()
        return out

    # -- internal --
    def _buka(self, occupied, t):
        self.active = True
        self.start_t = self.last_active_t = t
        self.order = []
        self._seen = set()
        self._catat(occupied)
        return [{"kind": "episode_mulai", "at": t}]

    def _catat(self, occupied):
        # rekam zona baru sesuai urutan muncul; urut kedalaman saat sefrekuensi -> deterministik
        for zone in sorted(occupied, key=depth_of):
            self.count[zone] += 1                # hitung kehadiran per zona (utk dominan-luar)
            if zone not in self._seen:
                self._seen.add(zone)
                self.order.append(zone)

    def _ringkas(self, end):
        return {"kind": "episode", "start": self.start_t, "end": end,
                "arah": self._arah(), "gates": list(self.order), "zona": sorted(self._seen)}

    def _arah(self):
        # DOMINAN DI LUAR: bila kehadiran mayoritas di jalan-utama (LUAR), ini PELEWAT
        # jalan, BUKAN masuk/keluar. Occlusion di batas pagar bikin anchor separuh-badan
        # (orang di jalan, terhalang) jitter ke zona-dalam beberapa frame -> jangan
        # terkecoh: bandingkan total frame luar vs dalam. (regresi: episode #2691).
        luar = sum(c for z, c in self.count.items() if z in LUAR)
        dalam = sum(c for z, c in self.count.items() if z not in LUAR)
        if luar > dalam:
            return "lewat"
        if len(self.order) < 2:
            return "lewat"
        depths = [depth_of(z) for z in self.order]
        awal, akhir, puncak = depths[0], depths[-1], max(depths)
        if akhir > awal:
            return "masuk"
        if akhir < awal:
            return "keluar"
        # Titik-ujung se-kedalaman: pakai PUNCAK. Menyentuh lapis lebih dalam
        # (mis. ambang pintu) lalu kembali ke zona baru sekedalaman awal =
        # sempat ke ambang rumah lalu keluar lagi -> KELUAR (jangan telan jadi
        # 'lewat'; inilah kasus keluar-rumah yg tadinya luput dari notif).
        if puncak > awal:
            return "keluar"
        return "lewat"


# SceneNotifier -> rules.py (portable/Colab; di-import di atas)


def _hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(frm, to, minutes):
    """menit-hari `minutes` ada di rentang [frm,to)? Dukung lewat tengah malam
    (frm>to, mis. '22:00'-'06:00')."""
    a, b = _hhmm(frm), _hhmm(to)
    if a <= b:
        return a <= minutes < b
    return minutes >= a or minutes < b


def notif_aktif(schedule, tags, epoch):
    """True bila NOTIFIKASI harus dikirim untuk event ber-`tags` pada `epoch`.
    schedule = {default:'aktif'|'senyap', rules:[{zones?, from, to, notif}]}.
    tags = himpunan nama zona/gerbang event. Aturan dievaluasi urut; yang COCOK
    TERAKHIR menang (kalau tak ada yang cocok -> default). Senyap = tetap rekam,
    cuma tak kirim Telegram (mode A)."""
    lt = time.localtime(epoch)
    minutes = lt.tm_hour * 60 + lt.tm_min
    hasil = schedule.get("default", "aktif")
    tags = set(tags)
    for rule in schedule.get("rules", []):
        rz = rule.get("zones")
        if rz and not (set(rz) & tags):
            continue
        if not in_window(rule.get("from", "00:00"), rule.get("to", "24:00"), minutes):
            continue
        hasil = rule.get("notif", "aktif")
    return hasil == "aktif"


SENTINEL = object()

def consume(q, handle):
    while True:
        ev = q.get()
        if ev is SENTINEL:
            break
        try:
            handle(ev)
        except Exception as e:
            print(f"[ERROR] handle gagal, event dilewati: {e}", flush=True)
