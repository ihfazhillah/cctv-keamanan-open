"""Shadow recorder berbasis SEGMEN ffmpeg (track terpisah, PROOF-OF-CONCEPT).

Tujuan: buktikan teknik "rekam RTSP -> segmen .ts bergulir di disk (audio ikut,
tanpa re-encode) -> potong klip A/V dari segmen" berjalan STABIL & bebas error,
BERDAMPINGAN dengan pipeline live tanpa menyentuhnya. Belum menggantikan apa pun.

Komponen:
  - supervisor ffmpeg: rekam RTSP -> out/segments/seg_<strftime>.ts (-c copy, -map 0)
    -> membawa video h264 + audio pcm_mulaw. Restart otomatis + backoff bila mati.
  - housekeeping: buang segmen lebih tua dari RETAIN_MIN.
  - self-test berkala: sambung beberapa segmen terakhir -> MP4 (audio -> AAC),
    ffprobe pastikan ADA stream video+audio & durasi>0. Catat PASS/FAIL.
  - health JSON (out/segrec-health.json) supaya pemantauan murah (baca 1 file kecil).

Jalankan: uv run --env-file .env segrec/run_segrec.py
Env: SEGREC_URL (default ch102 direct), SEG_DIR, SEG_TIME, RETAIN_MIN, SELFTEST_MIN.
"""
import os
import re
import json
import time
import glob
import signal
import threading
import subprocess
import urllib.parse
from collections import deque


# Sumber = NVR (bukan kamera direct): koneksi ke NVR tak menambah beban ke port
# kamera direct yg dipakai pipeline -> tak bikin pipeline stall (teruji).
# KREDENSIAL DARI ENV (.env, gitignored) — JANGAN hardcode password di sini.
# Set SEGREC_URL penuh, ATAU sediakan NVR_HOST/NVR_USER/NVR_PASS (+SEGREC_CHANNEL).
def _nvr_url_dari_env():
    host = os.environ.get("NVR_HOST", "")
    port = os.environ.get("NVR_RTSP_PORT", "554")
    user = os.environ.get("NVR_USER", "")
    pw = urllib.parse.quote(os.environ.get("NVR_PASS", ""), safe="")
    chan = os.environ.get("SEGREC_CHANNEL", "202")     # 2xx=taman, 1xx=garasi; x01=main x02=sub
    if not (host and user):
        return ""
    return f"rtsp://{user}:{pw}@{host}:{port}/Streaming/Channels/{chan}"


URL = os.environ.get("SEGREC_URL") or _nvr_url_dari_env()
SEG_DIR = os.environ.get("SEG_DIR", "out/segments")
SEG_TIME = int(os.environ.get("SEG_TIME", "4"))
SEG_MAX_GB = float(os.environ.get("SEG_MAX_GB", "50"))   # arsip bergulir maks (bukan 45mnt); hapus tertua saat lewat
SELFTEST_MIN = float(os.environ.get("SELFTEST_MIN", "5"))
HEALTH = os.environ.get("SEGREC_HEALTH", "out/segrec-health.json")
CLIP_TMP = os.environ.get("SEGREC_CLIPTMP", "out/segrec-selftest.mp4")
SEG_RE = re.compile(r"seg_(\d{8}_\d{6})\.ts$")

_stop = threading.Event()
_stderr = deque(maxlen=25)          # baris stderr ffmpeg terakhir (utk last_error)
_state = {"start": time.time(), "restarts": 0, "st_pass": 0, "st_fail": 0,
          "st_last": None, "st_detail": "", "last_error": ""}


def log(msg):
    print(f"[SEGREC] {msg}", flush=True)


def handle_term(signum, frame):
    _stop.set()


def seg_epoch(path):
    m = SEG_RE.search(path)
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1), "%Y%m%d_%H%M%S"))


_stats = {"seg_count": 0, "total_bytes": 0}      # di-refresh housekeeping (hindari walk seluruh arsip di hot-path)


def ensure_hour_dirs():
    """ffmpeg ini tak punya -strftime_mkdir -> buat subfolder jam-ini & jam-berikut
    sendiri (dipanggil tiap iterasi) supaya ffmpeg tak gagal saat lintas jam."""
    now = time.time()
    for t in (now, now + 3600):
        lt = time.localtime(t)
        os.makedirs(os.path.join(SEG_DIR, time.strftime("%Y%m%d", lt), time.strftime("%H", lt)),
                    exist_ok=True)


def _day_dirs():
    return sorted(glob.glob(os.path.join(SEG_DIR, "20*")))


def latest_hour_dirs(n=2):
    """n subfolder-jam terbaru — murah (utk self-test & freshness), tak scan seluruh arsip."""
    out = []
    for day in reversed(_day_dirs()):
        for h in sorted(glob.glob(os.path.join(day, "[0-2][0-9]")), reverse=True):
            out.append(h)
            if len(out) >= n:
                return list(reversed(out))
    return list(reversed(out))


def recent_segments():
    segs = []
    for d in latest_hour_dirs(2):
        segs.extend(glob.glob(os.path.join(d, "seg_*.ts")))
    segs.extend(glob.glob(os.path.join(SEG_DIR, "seg_*.ts")))     # legacy flat (transisi)
    return sorted(segs)


def newest_seg_age():
    segs = recent_segments()
    if not segs:
        return None
    return time.time() - os.path.getmtime(segs[-1])


def tulis_health(state_label, ffmpeg_pid):
    age = newest_seg_age()
    data = {
        "ts": round(time.time(), 1),
        "state": state_label,
        "uptime_s": round(time.time() - _state["start"], 1),
        "ffmpeg_pid": ffmpeg_pid,
        "restarts": _state["restarts"],
        "seg_count": _stats["seg_count"],
        "total_gb": round(_stats["total_bytes"] / 1e9, 2),
        "cap_gb": SEG_MAX_GB,
        "newest_seg_age_s": round(age, 1) if age is not None else None,
        "selftest": {"pass": _state["st_pass"], "fail": _state["st_fail"],
                     "last": _state["st_last"], "detail": _state["st_detail"]},
        "last_error": _state["last_error"][-400:],
    }
    tmp = HEALTH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, HEALTH)


def housekeeping():
    """Arsip bergulir SIZE-BASED: hapus segmen TERTUA saat total > SEG_MAX_GB.
    Walk sekali (sekalian refresh cache _stats). Sisakan >=50 segmen terbaru."""
    cap = SEG_MAX_GB * 1e9
    items = []
    total = 0
    for root, _, files in os.walk(SEG_DIR):
        for f in files:
            if f.endswith(".ts"):
                p = os.path.join(root, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                items.append((p, sz))
                total += sz
    items.sort()                                 # path memuat YYYYMMDD/HH -> urut kronologis
    i = 0
    while total > cap and i < len(items) - 50:
        p, sz = items[i]
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass
        i += 1
    _stats["seg_count"] = len(items) - i
    _stats["total_bytes"] = total
    if i:                                        # prune subfolder kosong
        for root, dirs, files in os.walk(SEG_DIR, topdown=False):
            if root != SEG_DIR and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass


def ffprobe_streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    j = json.loads(out.stdout or "{}")
    types = [s.get("codec_type") for s in j.get("streams", [])]
    dur = float(j.get("format", {}).get("duration", 0) or 0)
    return types, dur


def self_test():
    """Sambung beberapa segmen terakhir yang SUDAH selesai (bukan yang sedang
    ditulis) -> MP4 (video copy, audio pcm_mulaw -> AAC) -> ffprobe pastikan ADA
    stream video+audio & durasi>0. Sengaja pakai BANYAK segmen (~panjang episode)
    agar sekalian membuktikan klip episode/sesi yang panjang pun beraudio & utuh."""
    segs = recent_segments()
    if len(segs) < 4:
        return None                         # belum cukup segmen
    pakai = segs[-11:-1]                     # s.d. 10 segmen (~episode) minus yg terbaru (belum tuntas)
    lst = CLIP_TMP + ".txt"
    with open(lst, "w") as f:
        for p in pakai:
            f.write(f"file '{os.path.abspath(p)}'\n")
    # Segmen kini ber-timeline wallclock yang konsisten (lihat start_ffmpeg) -> concat
    # -c copy langsung menghasilkan MP4 durasi benar, TANPA re-encode.
    expected = len(pakai) * SEG_TIME
    r = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-movflags", "+faststart", CLIP_TMP],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return ("fail", f"ffmpeg rc={r.returncode}: {r.stderr.strip()[-200:]}")
    try:
        types, dur = ffprobe_streams(CLIP_TMP)
    except Exception as e:
        return ("fail", f"ffprobe: {e!r}")
    dur_ok = expected * 0.5 <= dur <= expected * 1.5     # durasi harus masuk akal, bukan asal >0
    ok = "video" in types and "audio" in types and dur_ok
    detail = f"streams={types} dur={dur:.1f}s (harap ~{expected}s) dari {len(pakai)} segmen"
    return ("pass" if ok else "fail", detail)


def stderr_reader(proc):
    for line in iter(proc.stderr.readline, ""):
        line = line.rstrip()
        if line:
            _stderr.append(line)
    proc.stderr.close()


def start_ffmpeg():
    os.makedirs(SEG_DIR, exist_ok=True)
    ensure_hour_dirs()                           # dir jam-ini & jam-berikut harus ada sebelum ffmpeg tulis
    # Audio kamera = pcm_mulaw, TIDAK bisa di-copy ke MPEG-TS (jadi bin_data ->
    # audio hilang). Transcode audio -> AAC (murah); video TETAP copy (tak ada
    # re-encode berat). Map hanya video+audio, abaikan stream data.
    # -use_wallclock_as_timestamps: KUNCI. Kamera kirim PTS absolut raksasa (~jam
    # uptime); audio di-transcode mulai 0 -> video(copy) & audio beda timeline ->
    # concat rusak (durasi ngawur). Stempel wallclock samakan timeline KEDUA stream
    # -> segmen bersih & concat -c copy menghasilkan durasi benar (tanpa re-encode).
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "warning",
           "-use_wallclock_as_timestamps", "1",
           "-rtsp_transport", "tcp", "-i", URL,
           "-map", "0:v", "-map", "0:a", "-c:v", "copy", "-c:a", "aac",
           "-f", "segment", "-segment_time", str(SEG_TIME),
           "-segment_format", "mpegts", "-reset_timestamps", "1",
           "-strftime", "1",              # subfolder per jam (arsip skala besar); dir dibuat ensure_hour_dirs
           os.path.join(SEG_DIR, "%Y%m%d", "%H", "seg_%Y%m%d_%H%M%S.ts")]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=stderr_reader, args=(proc,), daemon=True).start()
    log(f"ffmpeg mulai pid={proc.pid} url=...{URL[-24:]} seg={SEG_TIME}s")
    return proc


def main():
    signal.signal(signal.SIGTERM, handle_term)
    if not URL:
        log("FATAL: sumber kosong. Set SEGREC_URL atau NVR_HOST/NVR_USER/NVR_PASS di .env")
        raise SystemExit(2)
    os.makedirs(SEG_DIR, exist_ok=True)
    proc = start_ffmpeg()
    ff_start = time.time()                # kapan ffmpeg (re)start -> grace period stall-check
    next_house = 0.0
    next_test = time.time() + 60          # self-test pertama setelah 1 mnt (biar segmen terkumpul)
    backoff = 2

    while not _stop.is_set():
        # 1) supervise ffmpeg
        rc = proc.poll()
        if rc is not None:
            _state["last_error"] = " | ".join(list(_stderr)[-6:]) or f"ffmpeg exit rc={rc}"
            _state["restarts"] += 1
            log(f"ffmpeg mati rc={rc} restart#{_state['restarts']} backoff={backoff}s; err={_state['last_error'][-160:]}")
            tulis_health("down", None)
            if _stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
            proc = start_ffmpeg()
            ff_start = time.time()
            continue
        backoff = 2

        now = time.time()
        ensure_hour_dirs()                       # jamin subfolder jam-ini/berikut ada (lintas jam)
        # 2) housekeeping (size-cap) tiap 120s
        if now >= next_house:
            housekeeping()
            next_house = now + 120

        # 3) freshness: ffmpeg hidup tapi segmen macet? (beri GRACE sejak ffmpeg start,
        # supaya tak membunuh ffmpeg sebelum ia sempat menulis segmen segar pertama —
        # mis. setelah jeda/migrasi arsip: segmen terbaru "lama" tapi ffmpeg baru mulai)
        age = newest_seg_age()
        stale = (age is not None and age > SEG_TIME * 4 and (now - ff_start) > SEG_TIME * 4)
        if stale:
            _state["last_error"] = f"segmen macet (age={age:.0f}s) -> restart ffmpeg"
            log(_state["last_error"])
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            continue

        # 4) self-test berkala
        if now >= next_test:
            next_test = now + SELFTEST_MIN * 60
            try:
                res = self_test()
            except Exception as e:
                res = ("fail", f"exception {e!r}")
            if res is not None:
                status, detail = res
                _state["st_last"], _state["st_detail"] = status, detail
                _state["st_pass" if status == "pass" else "st_fail"] += 1
                log(f"self-test {status.upper()}: {detail}")

        tulis_health("degraded" if stale else "ok", proc.pid)
        _stop.wait(5)

    log("berhenti (SIGTERM)")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    tulis_health("stopped", None)


if __name__ == "__main__":
    main()
