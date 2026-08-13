"""Pipeline live RTSP -> event -> klip + notifikasi Telegram.

Dibaca sebagai RANGKAIAN STAGE eksplisit supaya mudah dipelajari & disisipi
(arah mission: identitas/ReID, perilaku, config). Satu frame mengalir:

    FrameSource  ->  DetectorTracker  ->  Occupancy  ->  RuleEngine   ->  ClipBuffer  ->  ClipRecorder
    (baca RTSP)      (YOLO+track)         (zona: set     (episode +        (pre/post       (encode klip +
                                          & per-track)   passage arah)     roll frame)      kirim Telegram)

Dua cabang di tiap frame:
  - "sekarang" (tulang punggung): detect -> occupancy -> rules -> trigger
  - "nanti" (buat klip): simpan frame bergulir; saat trigger lahir, kumpulkan
    window pre/post lalu serahkan ke antrean konsumen.
Konsumen (ClipRecorder) jalan di thread terpisah lewat queue -> encode & upload
tak menahan loop deteksi. Logika domain (tracker, buffer) ada di live.py (teruji).
"""

import time
import datetime
import os
import threading
import queue
import argparse
import json
import subprocess
import signal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import supervision as sv
from ultralytics import YOLO
import requests

from live import (FrameBuffer, EpisodeTracker, consume, SENTINEL, RuleEngine,
                  PendingNotifier, TransitAggregator, notif_aktif, SceneEpisode)
from encode import reencode_h264   # encoder auto (GPU->CPU) -> jalan juga di laptop tanpa GPU


TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]


def jam(t):
    return f"{datetime.datetime.fromtimestamp(t):%H:%M:%S}"


ZONE_EMOJI = {
    "teras": "🚪", "pintu": "🚪",
    "taman": "🌳",
    "dekat-kolam": "💧",
    "jalan-masuk": "🪜", "tangga": "🪜",
    "jalan-utama": "🛣️",
}


def zemoji(zone):
    return ZONE_EMOJI.get(zone, "📍")


# ── sinyal berhenti (systemd SIGTERM) ──────────────────────────────────────────
_stop = False


def handle_terminate(signum, frame):
    global _stop
    _stop = True




# ══ Telegram I/O ═══════════════════════════════════════════════════════════════
class Telegram:
    """Pengirim Telegram dengan timeout longgar + retry. photo/video menelan
    kegagalan akhir (jaringan tak boleh matikan pipeline); text melempar.

    timeout=10 dulu terlalu galak: upload video (lebih besar) sering kena
    ReadTimeout(read timeout=10) -> hanya snapshot kecil yang lolos, video
    "hilang". Kini pakai (connect, read) longgar per jenis + retry backoff.
    Catatan: gagal upload TIDAK menghilangkan klip -- file sudah tersimpan di
    out/live & event sudah di jsonl; ini cuma soal pengiriman."""

    def __init__(self, token, chat_id, retries=2):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.retries = retries

    def _upload(self, endpoint, field, path, caption, read_timeout):
        last = None
        for attempt in range(self.retries + 1):
            try:
                with open(path, "rb") as f:
                    r = requests.post(f"{self.base}/{endpoint}",
                                      data={"chat_id": self.chat_id, "caption": caption},
                                      files={field: f}, timeout=(10, read_timeout))
                r.raise_for_status()
                return True
            except Exception as e:
                last = e
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))     # backoff 2s, 4s
        print(f"[ERROR] kirim {field} gagal {self.retries + 1}x: {caption=} {path=} e={last!r}")
        return False

    def photo(self, path, caption):
        return self._upload("sendPhoto", "photo", path, caption, read_timeout=30)

    def video(self, path, caption):
        return self._upload("sendVideo", "video", path, caption, read_timeout=120)

    def text(self, text):
        r = requests.post(f"{self.base}/sendMessage",
                          data={"chat_id": self.chat_id, "text": text}, timeout=(10, 20))
        r.raise_for_status()


# ══ STAGE 1: sumber frame ══════════════════════════════════════════════════════
class FrameSource:
    """RTSP/file -> (t_walltime, frame). BUFFERSIZE=1 = ambil frame terbaru
    (drop-stale). Generator berhenti saat stream putus -> biar systemd restart."""

    def __init__(self, sumber):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.sumber = sumber
        self.cap = cv2.VideoCapture(sumber)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            raise RuntimeError(f"Gagal buka sumber: {sumber}")

    def frames(self):
        while True:
            ok, frame = self.cap.read()
            if not ok:
                return                      # stream putus -> hentikan generator
            yield time.time(), frame

    def release(self):
        self.cap.release()


# ══ STAGE 2: deteksi + tracking ════════════════════════════════════════════════
class DetectorTracker:
    """frame -> sv.Detections (sudah ber-track_id). Model di-load sekali & persist
    antar-frame (tracker butuh kontinuitas). conf rendah -> banyak false positive
    (parah malam IR); lihat --conf."""

    PERSON, CAT = 0, 15

    def __init__(self, model_name, conf=0.25, classes=(PERSON, CAT)):
        self.model = YOLO(model_name)
        self.conf = conf
        self.classes = list(classes)
        self.name = self.model.model_name

    def detect(self, frame):
        result = self.model.track(frame, persist=True, tracker="botsort_reid.yaml",
                                  verbose=False, conf=self.conf, classes=self.classes)[0]
        dets = sv.Detections.from_ultralytics(result)
        if dets.tracker_id is None:         # tak ada deteksi -> tracker_id None
            return sv.Detections.empty()
        return dets


# ══ STAGE 3: hunian zona ═══════════════════════════════════════════════════════
class Occupancy:
    """dets -> (occupied_set, tid_to_zone) dalam SATU lintasan trigger.
      - occupied_set : zone-centris, untuk EpisodeTracker ("ada orang di zona X")
      - tid_to_zone  : {track_id: zona|None}, untuk arah masuk/keluar per badan
    Anchor = BOTTOM_CENTER (satu titik) -> normalnya satu track di <=1 zona; bila
    zona tumpang-tindih, penulisan TERAKHIR menang. muat_zone menaruh `pintu` terakhir
    -> kaki di ambang (irisan pintu&teras) diklaim PINTU, bukan teras (krusial utk
    deteksi masuk/keluar rumah)."""

    def __init__(self, zones):
        self.zones = zones

    def of(self, dets):
        occupied = set()
        if dets.tracker_id is None:
            return occupied, {}
        tid_to_zone = {int(t): None for t in dets.tracker_id}
        for name, zone in self.zones.items():
            in_zone = zone.trigger(dets)
            if int(in_zone.sum()) > 0:
                occupied.add(name)
            for tid in dets.tracker_id[in_zone]:
                tid_to_zone[int(tid)] = name
        return occupied, tid_to_zone


def muat_zone(zone_file):
    """Baca poligon zona dari JSON -> {nama: sv.PolygonZone}.
    `pintu` ditaruh TERAKHIR agar menang di tumpang-tindih dgn teras (lihat Occupancy)."""
    zones = {}
    zone_data = json.loads(Path(zone_file).read_text())
    names = sorted(zone_data["zones"], key=lambda n: n == "pintu")   # pintu -> paling akhir
    for zone_name in names:
        points = zone_data["zones"][zone_name]
        zones[zone_name] = sv.PolygonZone(polygon=np.array(points, dtype=np.int32),
                                          triggering_anchors=[sv.Position.BOTTOM_CENTER])
    return zones


# ══ STAGE 5: buffer klip (cabang "nanti") ══════════════════════════════════════
class ClipBuffer:
    """Simpan frame bergulir (pre-roll) + kumpulkan frame per trigger sampai window
    pre/post lengkap, lalu keluarkan (due) untuk diserahkan ke antrean konsumen.
    Membungkus FrameBuffer (bergulir) + PendingNotifier (per-trigger), keduanya teruji."""

    def __init__(self, pre=5, post=5, keep_s=30):
        self.rolling = FrameBuffer(keep_s=keep_s)
        self.pending = PendingNotifier(pre, post)

    def observe(self, t, frame):
        self.pending.feed(t, frame)         # sambung post-roll ke trigger yang menunggu
        self.rolling.add(t, frame)          # simpan pre-roll bergulir

    def register(self, trigger, t):
        """Trigger lahir -> ambil pre-roll dari buffer bergulir, mulai kumpulkan post-roll.
        Pakai window yang SAMA dengan pending (_resolve_t0) -> tak ada kontradiksi."""
        t0 = self.pending._resolve_t0(trigger)
        self.pending.add(trigger, self.rolling.get(t0, t))

    def due(self, t):
        return self.pending.due(t)


class Arming:
    """Jadwal notifikasi (mode A: 'senyap' = tetap rekam+log, cuma tak kirim
    Telegram). Baca file dgn cache; refresh saat mtime berubah -> ganti jadwal
    dari viewer berlaku TANPA restart. File tak ada/rusak -> selalu AKTIF."""

    def __init__(self, path):
        self.path = path
        self.schedule = {"default": "aktif", "rules": []}
        self.mtime = None

    def _reload(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            self.schedule = {"default": "aktif", "rules": []}
            self.mtime = None
            return
        if m != self.mtime:
            try:
                self.schedule = json.loads(Path(self.path).read_text())
            except Exception as e:
                print(f"[PERINGATAN] arming file rusak, anggap AKTIF: {e}", flush=True)
                self.schedule = {"default": "aktif", "rules": []}
            self.mtime = m

    def should_notify(self, tags, epoch):
        self._reload()
        return notif_aktif(self.schedule, tags, epoch)


class DebugLog:
    """Diagnostik miss masuk/keluar: catat TRANSISI zona per track (person & kucing)
    -> `from -> to`, plus kelahiran track (from=∅). Dari deret ini kelihatan apakah
    penyeberangan teras<->halaman diamati SATU track (bersih), track PECAH (id baru
    lahir di sisi lain), atau tak ada track sama sekali (deteksi bolong).

    Toggle via file (default 'debug.on'): ADA = nyala. Dicek cache-mtime -> on/off
    LIVE tanpa restart (`touch debug.on` / `rm debug.on`). Hanya menulis saat berubah."""

    def __init__(self, toggle_path="debug.on", out_dir="out/debug"):
        self.toggle = toggle_path
        self.out_dir = out_dir
        self.on = False
        self.fh = None
        self.last = {}          # (kind, tid) -> zona terakhir

    def _refresh(self):
        exists = os.path.exists(self.toggle)
        if exists and not self.on:
            os.makedirs(self.out_dir, exist_ok=True)
            fn = os.path.join(self.out_dir, f"trans_{int(time.time())}.jsonl")
            self.fh = open(fn, "a")
            self.on = True
            self.last.clear()
            print(f"[DEBUG] transisi-log ON -> {fn}", flush=True)
        elif not exists and self.on:
            if self.fh:
                self.fh.close()
                self.fh = None
            self.on = False
            print("[DEBUG] transisi-log OFF", flush=True)

    def log(self, t, persons, cats):
        self._refresh()
        if not self.on:
            return
        wrote = False
        for kind, mapping in (("p", persons), ("c", cats)):
            seen = set()
            for tid, zone in mapping.items():
                seen.add((kind, tid))
                key = (kind, tid)
                prev = self.last.get(key, "∅")     # ∅ = track baru
                if zone != prev:
                    self.fh.write(json.dumps({"t": round(t, 2), "k": kind, "id": int(tid),
                                              "from": prev, "to": zone}, ensure_ascii=False) + "\n")
                    self.last[key] = zone
                    wrote = True
            # track yang HILANG dari frame ini -> catat mati (to=∅) sekali
            for key in [k for k in self.last if k[0] == kind and k not in seen]:
                self.fh.write(json.dumps({"t": round(t, 2), "k": kind, "id": key[1],
                                          "from": self.last[key], "to": "∅"}, ensure_ascii=False) + "\n")
                del self.last[key]
                wrote = True
        if wrote:
            self.fh.flush()


# ══ STAGE 6: perekam klip (konsumen di thread terpisah) ════════════════════════
class ClipRecorder:
    """Konsumen: pending trigger -> tulis event ke jsonl + bangun klip/snapshot +
    kirim Telegram. Encode (h264_nvenc) & upload berat -> sengaja di thread terpisah
    supaya tak menahan loop deteksi."""

    def __init__(self, telegram, out_dir="out/live", log_path="events-live.jsonl",
                 upload_workers=2, arming=None):
        self.tg = telegram
        self.out_dir = out_dir
        self.log_path = log_path
        self.arming = arming            # None -> selalu kirim (perilaku lama)
        # Upload dipisah ke worker pool: generate klip (ffmpeg) tetap di thread
        # konsumen, tapi upload Telegram yang lambat (retry/timeout) tak lagi
        # menahan klip berikutnya -> saat event beruntun, semua tetap terkirim.
        self.uploads = ThreadPoolExecutor(max_workers=upload_workers, thread_name_prefix="tg")

    # -- pesan caption --
    # Caption Set C (dua baris) + emoji per zona -> jenis & tempat kebaca sekejap.
    # Lead emoji = jenis: ⏳ loiter | 👤/🐈 close | 🟢 masuk | 🔴 keluar.
    def loiter_message(self, ev):
        loiter_s = ev["at"] - ev["start"]
        z = ev["zone"]
        aksi = "KUCING MANGKAL" if ev.get("species") == "kucing" else "ORANG BERLAMA"
        return f"⏳ {aksi} · {zemoji(z)} {z}\nsudah {loiter_s:.0f} detik · sejak {jam(ev['start'])}"

    def close_message(self, ev):
        dwell_s = ev["end"] - ev["start"]
        satuan = "detik"
        if dwell_s > 60:
            dwell_s = dwell_s / 60
            satuan = "menit"
        z = ev["zone"]
        lead, subj = ("🐈", "KUCING") if ev.get("species") == "kucing" else ("👤", "ORANG")
        return f"{lead} {subj} · {zemoji(z)} {z}\n{dwell_s:.1f} {satuan} · {jam(ev['start'])}–{jam(ev['end'])}"

    def passage_message(self, ev):    # tak dipakai (passage -> transit); jaga-jaga
        return f"📍 {ev['kind']} · {jam(ev['at'])}"

    def transit_message(self, ev):
        gates = ev.get("gates", [])
        masuk = ev["kind"] == "masuk"
        lead = "🟢" if masuk else "🔴"
        label = "MASUK" if masuk else "KELUAR"
        utama = "RUMAH" if "rumah" in gates else "PROPERTY"
        via = "property" if (utama == "RUMAH" and "property" in gates) else None
        baris2 = f"via {via} · {jam(ev['start'])}" if via else jam(ev["start"])
        return f"{lead} {label} {utama}\n{baris2}"

    def _caption(self, ev):
        match ev["kind"]:
            case "loiter": return self.loiter_message(ev)
            case "close": return self.close_message(ev)
            case _: return self.transit_message(ev)       # keluar / masuk

    # -- tulis event log --
    def _log(self, ev):
        baris = json.dumps(ev)
        print(baris, flush=True)
        with open(self.log_path, "a") as f:
            f.write(baris + "\n")

    # -- encode klip --
    def _reencode(self, raw_name, clip_name, duration):
        reencode_h264(raw_name, clip_name, duration)

    def _write_clip(self, ev, pending):
        frames = pending["frames"]
        t0, t1 = pending["t0"], pending["t1"]
        if not frames:
            print(f"[PERINGATAN] Tidak bisa mendapatkan frame dengan time {t0:.0f} - {t1:.0f}")
            return

        label = ev.get("zone") or "+".join(ev.get("gates", []))   # close/loiter=zona; transit=gerbang
        raw_name = os.path.join(self.out_dir, f"klip_{ev['kind']}_{label}_{t0:.0f}_{t1:.0f}-raw.mp4")
        final_name = os.path.join(self.out_dir, f"klip_{ev['kind']}_{label}_{t0:.0f}_{t1:.0f}.mp4")

        h, w = frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        duration = t1 - t0
        fps = max(1.0, len(frames) / duration if duration > 0 else 20.0)

        writer = cv2.VideoWriter(raw_name, fourcc, fps, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()

        self._reencode(raw_name, final_name, t1 - t0)
        os.remove(raw_name)                      # raw cuma untuk reencode; final sudah tersimpan
        # jadwal arming (mode A): senyap -> klip TETAP tersimpan, upload dilewati
        if self.arming and not self.arming.should_notify(self._tags(ev), self._event_time(ev)):
            print(f"[SENYAP] notif ditahan (jadwal): {ev['kind']} {sorted(self._tags(ev))}", flush=True)
            return
        self.uploads.submit(self.tg.video, final_name, self._caption(ev))   # kirim di background

    def _tags(self, ev):
        if ev.get("zone"):
            return {ev["zone"]}                  # close / loiter
        return set(ev.get("gates", []))          # transit keluar/masuk

    def _event_time(self, ev):
        return ev.get("start", ev.get("at", 0))

    CLIP_KINDS = {"close", "loiter", "keluar", "masuk"}
    TRANSIT_KINDS = {"keluar", "masuk"}

    def handle(self, pending):
        ev = pending["trigger"]
        # transit (keluar/masuk) = klip turunan dari passage yang SUDAH di-log -> jangan log ulang.
        if ev["kind"] not in self.TRANSIT_KINDS:
            self._log(ev)
        if ev["kind"] in self.CLIP_KINDS:
            self._write_clip(ev, pending)        # close/loiter/keluar/masuk -> klip video
        # passage (KELUAR/MASUK ...) -> cuma di-log, video-nya lewat transit

    def close(self):
        """Tunggu upload yang masih di antrean saat shutdown (best-effort)."""
        self.uploads.shutdown(wait=True)


# reencode_h264 dipindah ke encode.py (dipakai bersama notify & viewer, encoder auto).


# ══ STAGE 6b: perekam EPISODE (zone-driven, streaming) ═════════════════════════
class EpisodeRecorder:
    """Sisi-I/O untuk SceneEpisode: SATU video per episode — dari (pre-roll) orang
    muncul sampai (grace) semua hilang — ditulis STREAMING (tak menumpuk frame di RAM).

    Pemisahan tugas (Clean/Pragmatic): SceneEpisode memutuskan KAPAN buka/tutup
    (murni-logika, teruji di test_scene_episode). Kelas ini HANYA efek samping
    (VideoWriter, ffmpeg, disk, log), mengonsumsi event seperti aliran:

        observe(t, frame, occupied)
          'episode_mulai' -> buka writer + tumpahkan pre-roll
          (writer aktif)  -> tulis frame ini                 (streaming)
          'episode'       -> tutup writer -> reencode+nama (thread) -> log

    Reencode berat di-offload ke 1 worker supaya loop deteksi tak tertahan.
    Nama berkas membawa ringkasan: klip_episode_<mulai>_<selesai>_<arah>_<gate-gate>.mp4
    """

    def __init__(self, scene, out_dir="out/live", log_path="episodes-live.jsonl",
                 pre_s=3.0, fps=20.0):
        self.scene = scene            # SceneEpisode (otak keputusan)
        self.out_dir = out_dir
        self.log_path = log_path
        self.pre_s = pre_s            # detik pra-rekam sebelum orang muncul
        self.fps = fps
        self.rolling = FrameBuffer(keep_s=max(6, int(pre_s) + 2))
        self.writer = None
        self.raw_name = None
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="epi")

    def observe(self, t, frame, occupied):
        for ev in self.scene.update(occupied, t):
            if ev["kind"] == "episode_mulai":
                self._buka(t, frame)
            elif ev["kind"] == "episode":
                if self.writer is not None:
                    self.writer.write(frame)          # frame penutup ikut (post-roll)
                self._tutup(ev)
        if self.writer is not None:
            self.writer.write(frame)                  # streaming selama aktif
        self.rolling.add(t, frame)                    # SETELAH -> pre-roll tak dobel frame ini

    def flush(self, t):
        for ev in self.scene.flush(t):
            self._tutup(ev)

    # -- efek samping berkas --
    def _buka(self, t, frame):
        h, w = frame.shape[:2]
        self.raw_name = os.path.join(self.out_dir, f"klip_episode_{t:.0f}-raw.mp4")
        self.writer = cv2.VideoWriter(self.raw_name, cv2.VideoWriter_fourcc(*"mp4v"),
                                      self.fps, (w, h))
        for f in self.rolling.get(t - self.pre_s, t):    # pre-roll: frame SEBELUM t
            self.writer.write(f)

    def _tutup(self, ev):
        if self.writer is None:
            return
        self.writer.release()
        raw, self.writer, self.raw_name = self.raw_name, None, None
        gates = "-".join(ev["gates"]) or "diam"
        final = os.path.join(
            self.out_dir,
            f"klip_episode_{ev['start']:.0f}_{ev['end']:.0f}_{ev['arah']}_{gates}.mp4")
        self.jobs.submit(self._finalize, raw, final, ev)

    def _finalize(self, raw, final, ev):
        try:
            reencode_h264(raw, final)                 # encode semua frame (tanpa pangkas)
            os.remove(raw)
        except Exception as e:
            print(f"[PERINGATAN] episode encode gagal ({e}); simpan mentah", flush=True)
            try:
                os.replace(raw, final)
            except OSError:
                pass
        with open(self.log_path, "a") as f:
            f.write(json.dumps({**ev, "clip": os.path.basename(final)}) + "\n")
        print(f"[EPISODE] {ev['arah']} {ev['gates']} -> {os.path.basename(final)}", flush=True)

    def close(self):
        self.jobs.shutdown(wait=True)


# ══ Orkestrasi: rangkai stage jadi satu loop pipeline ══════════════════════════
def run_pipeline(source, detector, occupancy, engine, cat_engine, clips, transit, q,
                 debug=None, episodes=None):
    frame_no = 0
    last_hb_t = time.time()
    t = last_hb_t
    last_hb_frame = 0
    finish_reason = None

    print(f"[MULAI] {jam(last_hb_t)} sumber={source.sumber!r} model={detector.name}")

    try:
        for t, frame in source.frames():
            if _stop:
                finish_reason = "sigterm"
                break

            frame_no += 1
            clips.observe(t, frame)                          # cabang "nanti": simpan buat klip

            dets = detector.detect(frame)                    # cabang "sekarang": tulang punggung
            persons = dets[dets.class_id == DetectorTracker.PERSON]
            cats = dets[dets.class_id == DetectorTracker.CAT]

            occupied, tid_to_zone = occupancy.of(persons)
            occupied_cat, cat_tid_to_zone = occupancy.of(cats)

            if episodes:
                episodes.observe(t, frame, occupied)   # SATU video per episode (zone-driven)

            if debug:
                debug.log(t, tid_to_zone, cat_tid_to_zone)

            triggers = list(engine.update(occupied, tid_to_zone, t))
            for trig in cat_engine.update(occupied_cat, t):  # kucing: episode saja
                trig["species"] = "kucing"
                triggers.append(trig)

            for trig in triggers:
                transit.feed(trig)               # passage se-arah -> agregasi transit (abaikan non-passage)
                clips.register(trig, t)          # SEMUA di-log; close/loiter -> video; passage -> log saja
            for tt in transit.due(t):            # transit selesai -> SATU klip video keluar/masuk
                clips.register(tt, t)
            for pending in clips.due(t):
                q.put(pending)

            if t - last_hb_t >= 10:                          # sinyal hidup + kedalaman antrean
                fps = (frame_no - last_hb_frame) / (t - last_hb_t)
                print(f"[HIDUP] {jam(t)}, frame#{frame_no} fps={fps:.1f} q={q.qsize()}")
                last_hb_t = t
                last_hb_frame = frame_no
            if q.qsize() >= 20:
                print(f"[PERINGATAN] antrean menumpuk: q={q.qsize()}")
        else:
            finish_reason = "stream_putus"                   # generator habis = stream putus

    finally:
        if episodes:
            episodes.flush(t)                    # tutup episode terbuka -> klip terakhir keluar
        # flush: tutup episode/presence terbuka -> klip terakhir tak tersangkut selamanya
        sisa = list(engine.flush())
        for trig in cat_engine.flush():
            trig["species"] = "kucing"
            sisa.append(trig)
        for trig in sisa:
            transit.feed(trig)
            clips.register(trig, t)
        for tt in transit.flush():               # transit terbuka -> klip terakhir tak hilang
            clips.register(tt, t)
        for pending in clips.due(float("inf")):
            q.put(pending)

        q.put(SENTINEL)                                      # tanda berhenti buat konsumen

        print(f"[BERHENTI] {jam(t)} " + (f"sebab={finish_reason}" if finish_reason else "bersih"))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=str)
    parser.add_argument("--model", default="yolo11s.pt", type=str)
    parser.add_argument("--zone-file", default="zones.json", type=str)
    parser.add_argument("--out-dir", default="out/live", type=str,
                        help="folder tujuan klip & snapshot (jangan tumpuk di root). Dibuat bila belum ada.")
    parser.add_argument("--arming-file", default="arming.json", type=str,
                        help="jadwal notifikasi (senyap=tetap rekam, tak kirim TG). Dibaca live; edit dari viewer.")
    parser.add_argument("--debug-toggle", default="debug.on", type=str,
                        help="ADA file ini -> log transisi zona per track ke --debug-dir (diagnostik miss). on/off live.")
    parser.add_argument("--debug-dir", default="out/debug", type=str)
    parser.add_argument("--conf", default=0.25, type=float,
                        help="ambang confidence detektor. Dulu 0.05 (banjir false positive). "
                             "Naikkan lagi (mis. 0.35) bila malam masih noise; turunkan bila orang nyata di balik jaring pagar terlewat.")
    parser.add_argument("--exit-hysteresis", default=3.0, type=float)
    # enter-inertia: kehadiran harus bertahan >= sekian detik sebelum episode "sah"
    # (anti-kedip). Dulu 0.2 -> phantom 0.4s lolos. Naik ke 1.0. Harus < loiter_s.
    parser.add_argument("--enter-inertia", default=1.0, type=float)
    # min-presence: passage (masuk/keluar) diabaikan bila kehadiran lebih pendek dari ini.
    parser.add_argument("--min-presence", default=1.0, type=float)
    parser.add_argument("--loiter_s", default=30, type=float,
                        help="ambang dwell (detik) sebelum dianggap berlama-lama; harus > --enter-inertia")
    parser.add_argument("--ambang_s", default=3.0, type=float)
    # Episode scene-level (SATU video per kunjungan, dari muncul s.d. hilang). Zone-driven
    # -> kebal kedip/pecah ID. Lihat SceneEpisode / EpisodeRecorder.
    parser.add_argument("--episode-grace", default=6.0, type=float,
                        help="tutup episode setelah SEMUA zona kosong sekian detik (>= ambang_s biar oklusi tak memotong).")
    parser.add_argument("--episode-max", default=300.0, type=float,
                        help="potong episode lebih panjang dari ini (detik) jadi beberapa klip.")
    parser.add_argument("--episode-pre", default=3.0, type=float,
                        help="detik pra-rekam sebelum orang pertama muncul.")
    parser.add_argument("--no-episode", action="store_true",
                        help="matikan perekam episode scene-level.")
    return parser.parse_args()


def main():
    signal.signal(signal.SIGTERM, handle_terminate)
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # rakit stage (urutan = urutan aliran frame)
    detector = DetectorTracker(args.model, conf=args.conf)   # load model dulu (bisa lama)
    source = FrameSource(args.video)                         # lalu buka stream
    try:
        occupancy = Occupancy(muat_zone(args.zone_file))
        engine = RuleEngine(args.exit_hysteresis, args.loiter_s, args.ambang_s,
                            enter_inertia=args.enter_inertia, min_presence_s=args.min_presence)
        cat_engine = EpisodeTracker(args.exit_hysteresis, args.loiter_s, enter_inertia=args.enter_inertia)
        # buffer bergulir harus muat SELURUH masa berdiam loiter (detik) supaya
        # klip loiter mencakup [start, at+post], bukan cuma sekitar ambang.
        keep_s = max(30, int(args.loiter_s) + 10)
        clips = ClipBuffer(pre=5, post=5, keep_s=keep_s)
        # emit_delay >= post supaya post-roll transit sempat masuk buffer sebelum diklip.
        transit = TransitAggregator(emit_delay=6.0, join_gap=10.0)
        q = queue.Queue()

        recorder = ClipRecorder(Telegram(TOKEN, CHAT_ID), out_dir=args.out_dir,
                                arming=Arming(args.arming_file))
        consumer = threading.Thread(target=consume, args=(q, recorder.handle))
        consumer.start()                                     # konsumen hidup duluan

        episodes = None if args.no_episode else EpisodeRecorder(
            SceneEpisode(grace_s=args.episode_grace, max_s=args.episode_max),
            out_dir=args.out_dir, pre_s=args.episode_pre)

        debug = DebugLog(args.debug_toggle, args.debug_dir)
        run_pipeline(source, detector, occupancy, engine, cat_engine, clips, transit, q,
                     debug, episodes)
        consumer.join()
        recorder.close()          # tuntaskan upload yang masih di antrean
        if episodes:
            episodes.close()      # tuntaskan reencode episode yang masih di antrean
    finally:
        source.release()


if __name__ == "__main__":
    main()
