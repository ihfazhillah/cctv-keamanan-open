#!/usr/bin/env python3
"""Viewer web lokal untuk event CCTV: telusuri log detail, putar klip + snapshot,
cari/filter, putar-semua hasil search (stream berurutan), dan unduh.

    uv run pipeline/viewer.py        # server start + browser terbuka otomatis
    python3 pipeline/viewer.py       # (idem)

Tak perlu hapal port: default 8477 (bookmark-able), kalau sibuk pilih port bebas,
lalu browser dibuka otomatis ke URL yang benar. --no-open untuk mematikannya.

Nol dependensi (stdlib http.server + HTTP Range sendiri untuk seek/stream video).
Baca events-live.jsonl + out/live relatif --root (default: folder saat ini).
Alat baca-saja terpisah; aman jalan berbarengan dgn cctv.service (media di-refresh
tiap request).
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import datetime
import threading
import subprocess
import webbrowser
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from encode import h264_encoder   # encoder auto (GPU->CPU)

# segrec/ adalah sibling pipeline/ -> import pemotong segmen apa adanya (tanpa refactor)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "segrec"))
try:
    import cut as segcut            # pilih_segmen / cut(seg_dir,t0,t1,out)->path|None
except Exception:                   # arsip jadi non-aktif kalau modul tak ada
    segcut = None

mimetypes.add_type("video/mp2t", ".ts")   # segmen segrec (default tebak salah)

# diisi di main()
ROOT = "."
EVENTS_PATH = "events-live.jsonl"
MEDIA_DIR = "out/live"
ARMING_FILE = "arming.json"

EPOCH_MIN = 1_000_000_000        # ts di atas ini = wall-clock epoch (event live), bukan detik-file relatif
CLIP_KINDS = {"close", "loiter"}


# ══ Lapisan data ═══════════════════════════════════════════════════════════════
def load_events():
    """events-live.jsonl -> list event ternormalisasi (id, kind, zone, ts, lo, hi, dur, live)."""
    out = []
    try:
        f = open(os.path.join(ROOT, EVENTS_PATH))
    except FileNotFoundError:
        return out
    with f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = e.get("kind")
            if "start" in e and "end" in e:            # close
                ts, lo, hi = e["start"], e["start"], e["end"]
            elif "start" in e and "at" in e:           # loiter (alert @at, mulai @start)
                ts, lo, hi = e["at"], e["at"], e["at"]
            else:                                       # passage (@at)
                ts = lo = hi = e.get("at", 0.0)
            out.append({
                "id": i,
                "kind": kind,
                "zone": e.get("zone"),
                "species": e.get("species"),
                "ts": ts, "lo": lo, "hi": hi,
                "dur": (e["end"] - e["start"]) if ("start" in e and "end" in e) else None,
                "live": ts >= EPOCH_MIN,
                "raw": e,
            })
    return out


def scan_media():
    """Indeks out/live dari NAMA FILE: klip event per (kind,zone), snap per kind,
    dan klip TRANSIT (keluar/masuk) per arah."""
    clips = {}      # (kind, zone) -> list[(t0, t1, filename)]   (close/loiter)
    snaps = {}      # kind -> list[(at, filename)]
    transits = {}   # 'keluar'/'masuk' -> list[(t0, t1, filename)]
    n_clip = 0
    try:
        names = os.listdir(os.path.join(ROOT, MEDIA_DIR))
    except FileNotFoundError:
        return clips, snaps, transits, 0
    for name in names:
        if name.startswith("klip_") and name.endswith(".mp4") and not name.endswith("-raw.mp4"):
            parts = name[:-4].split("_")          # klip_{kind}_{zone|gerbang}_{t0}_{t1}.mp4
            if len(parts) != 5:
                continue
            _, kind, label, t0, t1 = parts
            try:
                t0, t1 = float(t0), float(t1)
            except ValueError:
                continue
            if kind in ("keluar", "masuk"):
                transits.setdefault(kind, []).append((t0, t1, name))
            else:
                clips.setdefault((kind, label), []).append((t0, t1, name))
                n_clip += 1
        elif name.startswith("snap_") and name.endswith(".jpg"):
            parts = name[:-4].split("_")          # snap_{KIND-dgn-dash}_{at}.jpg
            if len(parts) != 3:
                continue
            _, kind_dash, at = parts
            try:
                snaps.setdefault(kind_dash.replace("-", " "), []).append((float(at), name))
            except ValueError:
                continue
    return clips, snaps, transits, n_clip


def match_episode_media(ep, transits):
    """Cocokkan episode ke klip transit tersimpan: arah sama & window klip
    [t0,t1] MELINGKUPI span episode [start,end] (paling sempit)."""
    best = None
    for t0, t1, name in transits.get(ep["dir"], ()):
        if t0 <= ep["start"] + 1 and t1 >= ep["end"] - 1:
            width = t1 - t0
            if best is None or width < best[0]:
                best = (width, name)
    if best:
        return {"type": "video", "file": best[1], "url": "/media/" + quote(best[1])}
    return None


def match_media(ev, clips, snaps, transits):
    """Cocokkan event ke file media.
    - close/loiter -> klip [t0,t1] yang MELINGKUPI [lo,hi] (paling sempit).
    - passage (KELUAR/MASUK) -> klip TRANSIT se-arah yang window-nya memuat waktu
      passage (keluar & masuk SIMETRIS); fallback snapshot lama bila belum ada transit."""
    if not ev["live"]:
        return None
    if ev["kind"] in CLIP_KINDS:
        best = None
        for t0, t1, name in clips.get((ev["kind"], ev["zone"]), ()):
            if t0 <= ev["lo"] + 1 and t1 >= ev["hi"] - 1:
                width = t1 - t0
                if best is None or width < best[0]:
                    best = (width, name)
        if best:
            return {"type": "video", "file": best[1]}
    else:
        dirr = "keluar" if ev["kind"].startswith("KELUAR") else "masuk"
        best = None
        for t0, t1, name in transits.get(dirr, ()):        # klip transit (video)
            if t0 <= ev["ts"] + 1 and t1 >= ev["ts"] - 1:
                width = t1 - t0
                if best is None or width < best[0]:
                    best = (width, name)
        if best:
            return {"type": "video", "file": best[1]}
        best = None
        for at, name in snaps.get(ev["kind"], ()):          # fallback: snapshot lama
            d = abs(at - ev["ts"])
            if d < 2 and (best is None or d < best[0]):
                best = (d, name)
        if best:
            return {"type": "image", "file": best[1]}
    return None


def dt_str(ts):
    if ts < EPOCH_MIN:
        return f"+{ts:.1f}s"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def enrich_and_filter(qs):
    events = load_events()
    clips, snaps, transits, _ = scan_media()

    q = (qs.get("q", [""])[0] or "").strip().lower()
    kind = qs.get("kind", [""])[0]
    zone = qs.get("zone", [""])[0]
    frm = qs.get("from", [""])[0]
    to = qs.get("to", [""])[0]
    live_only = qs.get("live", ["1"])[0] == "1"
    media_only = qs.get("media", ["0"])[0] == "1"

    def day_epoch(s, end=False):
        try:
            d = datetime.datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        if end:
            d += datetime.timedelta(days=1)
        return d.timestamp()

    frm_ts = day_epoch(frm) if frm else None
    to_ts = day_epoch(to, end=True) if to else None

    rows = []
    for ev in events:
        if live_only and not ev["live"]:
            continue
        if kind and ev["kind"] != kind:
            continue
        if zone and ev["zone"] != zone:
            continue
        if frm_ts is not None and ev["ts"] < frm_ts:
            continue
        if to_ts is not None and ev["ts"] >= to_ts:
            continue
        if q:
            hay = f"{ev['kind']} {ev['zone'] or ''} {ev['species'] or ''} {dt_str(ev['ts'])}".lower()
            if q not in hay:
                continue
        media = match_media(ev, clips, snaps, transits)
        if media_only and not media:
            continue
        rows.append({
            "id": ev["id"],
            "kind": ev["kind"],
            "zone": ev["zone"],
            "species": ev["species"],
            "ts": ev["ts"],
            "lo": ev["lo"], "hi": ev["hi"],   # untuk pra-isi rentang tarikan NVR
            "when": dt_str(ev["ts"]),
            "dur": round(ev["dur"], 1) if ev["dur"] is not None else None,
            "media": ({"type": media["type"], "url": "/media/" + quote(media["file"]),
                       "file": media["file"]} if media else None),
        })

    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def meta():
    events = load_events()
    _, _, _, n_clip = scan_media()
    kinds = sorted({e["kind"] for e in events if e["kind"]})
    zones = sorted({e["zone"] for e in events if e["zone"]})
    days = sorted({dt_str(e["ts"])[:10] for e in events if e["live"]}, reverse=True)
    live_n = sum(1 for e in events if e["live"])
    return {"kinds": kinds, "zones": zones, "days": days,
            "total": len(events), "live": live_n, "clips": n_clip,
            "nvr": NVR["ok"], "segrec": SEGREC["ok"], "segMax": SEGREC["max_s"]}


# ══ NVR: tarik footage penuh per rentang waktu (Hikvision RTSP playback) ═════════
# Klip tersimpan cuma potongan pre/post; satu episode nyata sering kepotong jadi
# fragmen. Fitur ini menarik footage utuh [start,end] langsung dari rekaman NVR
# (mainstream full-res) untuk bahan develop / contoh. Di-re-encode (bukan -c copy):
# lihat til/2026-07-16-prefer-rerender-daripada-hanya-copy.md.
NVR = {"host": None, "port": "554", "user": None, "pass": None,
       "track": "201", "dir": "out/nvr", "max_s": 600, "ok": False}


def _read_env_file(root):
    env = {}
    try:
        for line in open(os.path.join(root, ".env")):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def load_nvr(root, track, out_dir, max_s):
    """Isi NVR{} dari environment / .env. host+user+pass wajib agar aktif."""
    env = _read_env_file(root)
    get = lambda k: os.environ.get(k) or env.get(k)
    NVR.update({
        "host": get("NVR_HOST"), "port": get("NVR_RTSP_PORT") or "554",
        "user": get("NVR_USER"), "pass": get("NVR_PASS"),
        "track": track, "dir": out_dir, "max_s": max_s,
    })
    NVR["ok"] = bool(NVR["host"] and NVR["user"] and NVR["pass"])


def _hik(epoch):
    # Hikvision starttime/endtime: waktu LOKAL perangkat, format YYYYMMDDTHHMMSSZ.
    return time.strftime("%Y%m%dT%H%M%SZ", time.localtime(epoch))


def nvr_grab(start, end):
    if not NVR["ok"]:
        return {"ok": False, "error": "NVR belum dikonfigurasi (.env: NVR_HOST/NVR_USER/NVR_PASS)"}
    dur = end - start
    if dur <= 0:
        return {"ok": False, "error": "rentang tidak valid (end <= start)"}
    if dur > NVR["max_s"]:
        return {"ok": False, "error": f"rentang {dur:.0f}s melebihi batas {NVR['max_s']}s (--nvr-max-seconds)"}

    os.makedirs(os.path.join(ROOT, NVR["dir"]), exist_ok=True)
    fname = f"nvr_{NVR['track']}_{int(start)}_{int(end)}.mp4"
    outpath = os.path.join(ROOT, NVR["dir"], fname)
    url_out = "/nvr/" + quote(fname)
    if os.path.isfile(outpath) and os.path.getsize(outpath) > 1000:
        return {"ok": True, "file": fname, "url": url_out, "seconds": round(dur, 1), "cached": True}

    user = quote(NVR["user"], safe=""); pw = quote(NVR["pass"], safe="")
    src = (f"rtsp://{user}:{pw}@{NVR['host']}:{NVR['port']}"
           f"/Streaming/tracks/{NVR['track']}?starttime={_hik(start)}&endtime={_hik(end)}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-rtsp_transport", "tcp", "-i", src, "-t", str(int(dur) + 1),
           "-c:v", h264_encoder(), "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", "-y", outpath]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(90, dur * 4 + 30))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg timeout (NVR lambat / rentang terlalu besar)"}

    if os.path.isfile(outpath) and os.path.getsize(outpath) > 1000:
        return {"ok": True, "file": fname, "url": url_out,
                "seconds": round(dur, 1), "size": os.path.getsize(outpath)}
    last = (r.stderr or "").strip().splitlines()
    return {"ok": False, "error": (last[-1] if last else f"ffmpeg gagal (exit {r.returncode})")}


def nvr_grab_from_qs(qs):
    try:
        start = float(qs.get("start", [""])[0]); end = float(qs.get("end", [""])[0])
    except (ValueError, IndexError):
        return {"ok": False, "error": "parameter start/end tidak valid"}
    return nvr_grab(start, end)


# ══ Arsip segrec: telusur rekaman penuh per waktu (segmen .ts bergulir) ══════════
# segrec merekam A/V utuh -> out/segments/YYYYMMDD/HH/seg_*.ts. Fitur ini menjadikan
# arsip itu bisa ditelusuri seperti NVR: pilih tanggal->jam->rentang, potong on-demand
# via segrec/cut.py (-c copy, langsung playable+seekable), sajikan lewat _serve().
SEGREC = {"seg_dir": "out/segments", "clip_dir": "out/segclip", "max_s": 300,
          "seg_time": 4, "ok": False}


def load_segrec(root, seg_dir, clip_dir, max_s):
    SEGREC.update({"seg_dir": seg_dir, "clip_dir": clip_dir, "max_s": max_s})
    SEGREC["ok"] = bool(segcut) and os.path.isdir(os.path.join(root, seg_dir))


def seg_index():
    """Hari->jam tersedia di arsip (dibaca dari struktur folder, murah tiap request)."""
    out = {"ok": SEGREC["ok"], "tz": time.strftime("%Z"), "days": []}
    if not SEGREC["ok"]:
        return out
    base = os.path.join(ROOT, SEGREC["seg_dir"])
    try:
        days = sorted((d for d in os.listdir(base) if re.fullmatch(r"\d{8}", d)), reverse=True)
    except FileNotFoundError:
        return out
    for day in days:
        dday = os.path.join(base, day)
        hours = []
        try:
            hnames = sorted(os.listdir(dday))
        except OSError:
            continue
        for h in hnames:
            hd = os.path.join(dday, h)
            if not (re.fullmatch(r"\d{2}", h) and os.path.isdir(hd)):
                continue
            try:
                n = sum(1 for f in os.listdir(hd) if f.startswith("seg_") and f.endswith(".ts"))
            except OSError:
                n = 0
            if n:
                hours.append({"h": h, "n": n})
        if hours:
            out["days"].append({"day": day, "hours": hours})
    return out


def _seg_epoch_of(day, hms):
    """day='YYYYMMDD', hms='HH:MM:SS' -> epoch LOKAL (mktime menormalkan overflow
    menit/detik -> rentang lintas-jam aman). Otoritas TZ = server, samakan dgn segrec."""
    parts = [int(x) for x in hms.split(":")] + [0, 0]
    return time.mktime((int(day[0:4]), int(day[4:6]), int(day[6:8]),
                        parts[0], parts[1], parts[2], 0, 0, -1))


def seg_grab(start, end):
    if not SEGREC["ok"]:
        return {"ok": False, "error": "arsip segrec tak tersedia (out/segments / modul cut)"}
    dur = end - start
    if dur <= 0:
        return {"ok": False, "error": "rentang tidak valid (end <= start)"}
    if dur > SEGREC["max_s"]:
        return {"ok": False, "error": f"rentang {dur:.0f}s melebihi batas {SEGREC['max_s']}s (--seg-max-seconds)"}

    clip_dir = os.path.join(ROOT, SEGREC["clip_dir"])
    os.makedirs(clip_dir, exist_ok=True)
    fname = f"seg_{int(start)}_{int(end)}.mp4"
    outpath = os.path.join(clip_dir, fname)
    url_out = "/segclip/" + quote(fname)
    if os.path.isfile(outpath) and os.path.getsize(outpath) > 1000:
        return {"ok": True, "file": fname, "url": url_out, "seconds": round(dur, 1), "cached": True}

    seg_dir = os.path.join(ROOT, SEGREC["seg_dir"])
    try:
        res = segcut.cut(seg_dir, start, end, outpath)
    except Exception as e:
        return {"ok": False, "error": f"cut gagal: {e}"}
    if res and os.path.isfile(outpath) and os.path.getsize(outpath) > 1000:
        return {"ok": True, "file": fname, "url": url_out,
                "seconds": round(dur, 1), "size": os.path.getsize(outpath)}
    return {"ok": False, "error": "tak ada segmen menutupi rentang (di luar retensi / segrec mati?)"}


def seg_grab_from_qs(qs):
    day = qs.get("day", [""])[0]
    start_s = qs.get("start", [""])[0]
    end_s = qs.get("end", [""])[0]
    if not (re.fullmatch(r"\d{8}", day or "") and start_s and end_s):
        return {"ok": False, "error": "parameter day/start/end tidak valid"}
    try:
        start = _seg_epoch_of(day, start_s)
        end = _seg_epoch_of(day, end_s)
    except (ValueError, IndexError):
        return {"ok": False, "error": "format waktu tidak valid (HH:MM:SS)"}
    return seg_grab(start, end)


def seg_hls(day, hour):
    """Playlist HLS VOD utk satu jam: menunjuk seg_*.ts LANGSUNG (tanpa re-mux) ->
    putar jam penuh + seek instan di browser (via hls.js). Segmen segrec sudah
    MPEG-TS h264+AAC = segmen HLS apa adanya. EXTINF nominal seg_time; jeda (segrec
    restart) -> #EXT-X-DISCONTINUITY. Kembalikan teks m3u8 atau None."""
    if not SEGREC["ok"]:
        return None
    hd = os.path.join(ROOT, SEGREC["seg_dir"], day, hour)
    if not os.path.isdir(hd):
        return None
    try:
        names = sorted(n for n in os.listdir(hd) if n.startswith("seg_") and n.endswith(".ts"))
    except OSError:
        return None
    if not names:
        return None
    seg_time = SEGREC["seg_time"]
    eps = [segcut.seg_epoch(os.path.join(hd, n)) for n in names]
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(seg_time) + 1}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for i, n in enumerate(names):
        if i and eps[i] is not None and eps[i - 1] is not None and (eps[i] - eps[i - 1]) > seg_time * 1.5:
            lines.append("#EXT-X-DISCONTINUITY")           # jeda rekaman -> reset timeline decoder
        lines.append(f"#EXTINF:{float(seg_time):.3f},")
        lines.append(f"/seg/ts/{day}/{hour}/{quote(n)}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ══ Episode: kelompokkan passage SE-ARAH jadi satu transit ══════════════════════
# "keluar" nyata = KELUAR rumah -> KELUAR property (satu gerak keluar); "masuk" =
# MASUK property -> MASUK rumah. Klip tersimpan memotong ini jadi fragmen; episode
# menyatukan passage se-arah yang berdekatan supaya footage penuhnya bisa ditarik.
GATE_DIR = {"KELUAR rumah": "keluar", "KELUAR property": "keluar",
            "MASUK rumah": "masuk", "MASUK property": "masuk"}


def episodes(gap=20.0):
    passages = [e for e in load_events() if e["live"] and e["kind"] in GATE_DIR]
    passages.sort(key=lambda e: e["ts"])

    clusters = []
    cur = None
    for e in passages:
        d = GATE_DIR[e["kind"]]
        if cur and cur["dir"] == d and (e["ts"] - cur["end"]) <= gap:
            cur["end"] = e["ts"]
            cur["kinds"].append(e["kind"])
        else:
            if cur:
                clusters.append(cur)
            cur = {"dir": d, "start": e["ts"], "end": e["ts"], "kinds": [e["kind"]]}
    if cur:
        clusters.append(cur)

    _, _, transits, _ = scan_media()
    out = []
    for c in clusters:
        gates = sorted({k.split(" ", 1)[1] for k in c["kinds"]})   # rumah / property
        ep = {"dir": c["dir"], "gates": gates,
              "start": c["start"], "end": c["end"],
              "when": dt_str(c["start"]),
              "dur": round(c["end"] - c["start"], 1),
              "count": len(c["kinds"]), "kinds": c["kinds"]}
        ep["media"] = match_episode_media(ep, transits)            # klip transit tersimpan (bila ada)
        out.append(ep)
    out.sort(key=lambda x: x["start"], reverse=True)
    for i, o in enumerate(out):
        o["id"] = i
    return out


def episodes_from_qs(qs):
    try:
        gap = float(qs.get("gap", ["20"])[0])
    except (ValueError, IndexError):
        gap = 20.0
    return episodes(gap)


# ══ Arming: jadwal notifikasi (dibaca pipeline live) ════════════════════════════
def read_arming():
    try:
        return json.loads(Path(os.path.join(ROOT, ARMING_FILE)).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"default": "aktif", "rules": []}


def write_arming(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("rules"), list):
        raise ValueError("format arming tidak valid (butuh {default, rules[]})")
    if obj.get("default") not in ("aktif", "senyap"):
        raise ValueError("default harus 'aktif' atau 'senyap'")
    Path(os.path.join(ROOT, ARMING_FILE)).write_text(json.dumps(obj, indent=2, ensure_ascii=False))


# ══ HTTP ═══════════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            self._html()
        elif u.path == "/api/events":
            self._json(enrich_and_filter(qs))
        elif u.path == "/api/meta":
            self._json(meta())
        elif u.path == "/api/episodes":
            self._json(episodes_from_qs(qs))
        elif u.path == "/api/arming":
            self._json(read_arming())
        elif u.path == "/api/nvr/grab":
            self._json(nvr_grab_from_qs(qs))
        elif u.path == "/api/seg/index":
            self._json(seg_index())
        elif u.path == "/api/seg/clip":
            self._json(seg_grab_from_qs(qs))
        elif u.path == "/api/seg/hls":
            self._seg_hls(qs)
        elif u.path.startswith("/seg/ts/"):
            self._seg_ts(u.path[len("/seg/ts/"):])
        elif u.path.startswith("/static/"):
            self._serve(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
                        u.path[len("/static/"):])
        elif u.path.startswith("/media/"):
            self._serve(os.path.join(ROOT, MEDIA_DIR), u.path[len("/media/"):],
                        download=(qs.get("download", ["0"])[0] == "1"))
        elif u.path.startswith("/nvr/"):
            self._serve(os.path.join(ROOT, NVR["dir"]), u.path[len("/nvr/"):],
                        download=(qs.get("download", ["0"])[0] == "1"))
        elif u.path.startswith("/segclip/"):
            self._serve(os.path.join(ROOT, SEGREC["clip_dir"]), u.path[len("/segclip/"):],
                        download=(qs.get("download", ["0"])[0] == "1"))
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/arming":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                write_arming(json.loads(body))
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, code=400)
        else:
            self.send_error(404)

    def _html(self):
        body = INDEX_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, base_dir, name, download=False):
        name = os.path.basename(unquote(name))   # cegah path traversal
        self._serve_path(os.path.join(base_dir, name), name, download)

    def _serve_path(self, path, name, download=False):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"

        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                partial = start <= end
        length = end - start + 1

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()

        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _seg_hls(self, qs):
        day = qs.get("day", [""])[0]; hour = qs.get("hour", [""])[0]
        if not (re.fullmatch(r"\d{8}", day or "") and re.fullmatch(r"\d{2}", hour or "")):
            self.send_error(400)
            return
        pl = seg_hls(day, hour)
        if not pl:
            self.send_error(404)
            return
        body = pl.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _seg_ts(self, sub):
        parts = unquote(sub).split("/")
        if len(parts) != 3:
            self.send_error(404)
            return
        day, hour, name = parts
        name = os.path.basename(name)
        if not (re.fullmatch(r"\d{8}", day) and re.fullmatch(r"\d{2}", hour)
                and re.fullmatch(r"seg_\d{8}_\d{6}\.ts", name)):
            self.send_error(404)
            return
        self._serve_path(os.path.join(ROOT, SEGREC["seg_dir"], day, hour, name), name)

    do_HEAD = do_GET


INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCTV — Viewer Event</title>
<style>
  :root {
    /* light — "pemantauan siang" */
    --bg:#e9edf1; --panel:#ffffff; --panel2:#f3f6f9; --line:#d5dce4;
    --fg:#1a212b; --dim:#5c6a7a; --faint:#8a97a6;
    --accent:#1f6f9e; --accent-weak:rgba(31,111,158,.12);
    --ev-enter:#1c8f5a; --ev-enter-bg:rgba(28,143,90,.13);
    --ev-exit:#c74a3f;  --ev-exit-bg:rgba(199,74,63,.12);
    --ev-dwell:#9a6a18; --ev-dwell-bg:rgba(154,106,24,.14);
    --ev-loiter:#7a4fc0;--ev-loiter-bg:rgba(122,79,192,.13);
    --ev-cat:#0f7d7d;
    --screen:#0c0f14; --scan:rgba(255,255,255,.03);
    --shadow:0 1px 2px rgba(20,30,45,.06),0 8px 24px rgba(20,30,45,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0e131a; --panel:#161c25; --panel2:#1c232e; --line:#29323e;
      --fg:#e6ebf2; --dim:#8b98a9; --faint:#5d6a7a;
      --accent:#4ea3d9; --accent-weak:rgba(78,163,217,.16);
      --ev-enter:#3fbf87; --ev-enter-bg:rgba(63,191,135,.14);
      --ev-exit:#e8746b;  --ev-exit-bg:rgba(232,116,107,.15);
      --ev-dwell:#e0a44a; --ev-dwell-bg:rgba(224,164,74,.15);
      --ev-loiter:#b088e0;--ev-loiter-bg:rgba(176,136,224,.15);
      --ev-cat:#35c2c2;
      --screen:#05070a; --scan:rgba(255,255,255,.028);
      --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --bg:#0e131a; --panel:#161c25; --panel2:#1c232e; --line:#29323e;
    --fg:#e6ebf2; --dim:#8b98a9; --faint:#5d6a7a;
    --accent:#4ea3d9; --accent-weak:rgba(78,163,217,.16);
    --ev-enter:#3fbf87; --ev-enter-bg:rgba(63,191,135,.14);
    --ev-exit:#e8746b;  --ev-exit-bg:rgba(232,116,107,.15);
    --ev-dwell:#e0a44a; --ev-dwell-bg:rgba(224,164,74,.15);
    --ev-loiter:#b088e0;--ev-loiter-bg:rgba(176,136,224,.15);
    --ev-cat:#35c2c2;
    --screen:#05070a; --scan:rgba(255,255,255,.028);
    --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
  }

  * { box-sizing:border-box; }
  [hidden] { display:none !important; }   /* menang atas display:flex/grid saat disembunyikan */
  html,body { height:100%; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .mono { font-family:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace; font-variant-numeric:tabular-nums; }
  .eyebrow { text-transform:uppercase; letter-spacing:.09em; font-size:10.5px; font-weight:600; color:var(--faint); }

  header {
    display:flex; align-items:center; gap:14px; padding:11px 18px;
    background:var(--panel); border-bottom:1px solid var(--line);
  }
  .brand { display:flex; align-items:baseline; gap:9px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--ev-exit); box-shadow:0 0 0 3px var(--ev-exit-bg); }
  .brand h1 { font-size:15px; font-weight:650; margin:0; letter-spacing:-.01em; }
  .brand .sub { color:var(--faint); font-size:11.5px; }
  header .spacer { flex:1; }
  .kpis { display:flex; gap:22px; }
  .kpi { text-align:right; }
  .kpi b { font-size:16px; font-weight:650; }
  .kpi span { display:block; }

  .console { display:grid; grid-template-columns:minmax(420px,44%) 1fr; height:calc(100vh - 55px); }
  @media (max-width:900px){ .console{ grid-template-columns:1fr; height:auto; } }

  .left { display:flex; flex-direction:column; border-right:1px solid var(--line); min-height:0; }
  .filters { padding:12px 14px; background:var(--panel); border-bottom:1px solid var(--line); display:flex; flex-direction:column; gap:10px; }
  .search { position:relative; }
  .search svg { position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--faint); }
  .search input { width:100%; padding:9px 12px 9px 34px; }
  input,select { background:var(--panel2); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:13px; font-family:inherit; }
  input:focus,select:focus,button:focus-visible,tr:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .seg { display:inline-flex; align-self:flex-start; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .seg button { background:transparent; color:var(--dim); border:0; border-radius:0; padding:6px 15px; font-weight:600; }
  .seg button[aria-selected="true"] { background:var(--accent); color:#fff; }
  .num { width:62px; padding:5px 7px; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .chip {
    cursor:pointer; user-select:none; padding:4px 11px; border-radius:99px; font-size:12px; font-weight:600;
    border:1px solid var(--line); background:transparent; color:var(--dim);
  }
  .chip[aria-pressed="true"] { border-color:transparent; }
  .chip.k-enter[aria-pressed="true"]{ background:var(--ev-enter-bg); color:var(--ev-enter); }
  .chip.k-exit[aria-pressed="true"]{ background:var(--ev-exit-bg); color:var(--ev-exit); }
  .chip.k-dwell[aria-pressed="true"]{ background:var(--ev-dwell-bg); color:var(--ev-dwell); }
  .chip.k-loiter[aria-pressed="true"]{ background:var(--ev-loiter-bg); color:var(--ev-loiter); }
  .frow { display:flex; gap:8px; align-items:center; }
  .frow > * { flex:1; }
  .toolbar { display:flex; align-items:center; gap:10px; padding:9px 14px; background:var(--panel); border-bottom:1px solid var(--line); }
  button {
    cursor:pointer; font-family:inherit; font-size:13px; font-weight:600;
    background:var(--accent); color:#fff; border:0; border-radius:8px; padding:8px 13px;
    display:inline-flex; align-items:center; gap:7px;
  }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--fg); }
  button.ghost:hover { background:var(--panel2); filter:none; }
  .count { color:var(--faint); font-size:12px; margin-left:auto; }

  .log { overflow:auto; flex:1; min-height:0; }
  table { width:100%; border-collapse:collapse; }
  thead th {
    position:sticky; top:0; z-index:1; background:var(--panel);
    text-align:left; padding:8px 14px; font-size:10.5px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--faint); font-weight:600; border-bottom:1px solid var(--line);
  }
  tbody td { padding:9px 14px; border-bottom:1px solid var(--line); font-size:13px; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:var(--panel2); }
  tbody tr[aria-selected="true"] { background:var(--accent-weak); box-shadow:inset 3px 0 0 var(--accent); }
  .tag { display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border-radius:99px; font-size:11.5px; font-weight:650; white-space:nowrap; }
  .tag.k-enter{ background:var(--ev-enter-bg); color:var(--ev-enter); }
  .tag.k-exit{ background:var(--ev-exit-bg); color:var(--ev-exit); }
  .tag.k-dwell{ background:var(--ev-dwell-bg); color:var(--ev-dwell); }
  .tag.k-loiter{ background:var(--ev-loiter-bg); color:var(--ev-loiter); }
  .tag.k-plain{ background:var(--panel2); color:var(--dim); }
  .zone { color:var(--fg); }
  .cat { color:var(--ev-cat); font-weight:600; }
  .med { color:var(--faint); }
  .dur { color:var(--dim); }

  .right { display:flex; flex-direction:column; min-height:0; padding:16px; gap:14px; overflow:auto; }
  .stage { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; box-shadow:var(--shadow); }
  .screen { position:relative; aspect-ratio:16/9; background:var(--screen); display:flex; align-items:center; justify-content:center; }
  .screen video, .screen img { width:100%; height:100%; object-fit:contain; display:block; background:var(--screen); }
  .scan { position:absolute; inset:0; pointer-events:none; background:repeating-linear-gradient(var(--scan) 0 1px, transparent 1px 3px); }
  .rec { position:absolute; top:10px; left:12px; display:flex; align-items:center; gap:6px; font-size:11px; color:#e8746b; font-weight:600; letter-spacing:.04em; text-shadow:0 1px 2px #000; z-index:2; }
  .rec .b { width:8px; height:8px; border-radius:50%; background:#e8746b; }
  .osd-tr { position:absolute; top:10px; right:12px; font-size:11.5px; color:#d7e0ea; text-shadow:0 1px 2px #000; z-index:2; }
  .stage .cap { display:flex; align-items:center; gap:10px; padding:8px 12px; border-top:1px solid var(--line); }
  .stage .cap .fn { color:var(--faint); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .stage .cap a { margin-left:auto; color:var(--accent); text-decoration:none; font-weight:600; font-size:12.5px; white-space:nowrap; }
  .stage .cap a:hover { text-decoration:underline; }

  .detail { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow); }
  .detail h2 { margin:0 0 10px; font-size:15px; display:flex; align-items:center; gap:9px; }
  .kv { display:grid; grid-template-columns:110px 1fr; gap:7px 14px; margin:0; }
  .kv dt { color:var(--dim); font-size:12.5px; }
  .kv dd { margin:0; font-size:13px; }

  .nvr .qhead { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
  .nvr .nvrctl { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin:10px 0; }
  .nvr label { color:var(--dim); font-size:12.5px; }
  .nvr .num { width:62px; padding:5px 7px; }
  .nvr .nvract { display:flex; align-items:center; gap:10px; }
  .nvr #nvrResult { margin-top:10px; }
  .nvr .done { display:flex; align-items:center; gap:12px; font-size:12.5px; flex-wrap:wrap; }
  .nvr a.dl { color:var(--accent); text-decoration:none; font-weight:600; }
  .nvr a.dl:hover { text-decoration:underline; }
  .spinner { width:13px; height:13px; border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%; display:inline-block; vertical-align:-2px; animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion:reduce){ .spinner{ animation:none; } }

  .queue { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 14px; box-shadow:var(--shadow); }
  .queue .qhead { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .queue ol { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:2px; max-height:180px; overflow:auto; }
  .queue li { display:flex; align-items:center; gap:9px; padding:6px 8px; border-radius:7px; font-size:12.5px; cursor:pointer; }
  .queue li:hover { background:var(--panel2); }
  .queue li.on { background:var(--accent-weak); }
  .queue li .n { color:var(--faint); width:20px; }
  .queue .empty { color:var(--faint); font-size:12.5px; padding:6px 2px; }

  .placeholder { color:var(--faint); text-align:center; padding:40px 16px; }
  .note { font-size:11.5px; color:var(--faint); }

  .hbtn { background:transparent; border:1px solid var(--line); color:var(--fg); font-weight:600; }
  .hbtn:hover { background:var(--panel2); filter:none; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.5); display:flex; align-items:center; justify-content:center; z-index:10; }
  .modal-box { background:var(--panel); border:1px solid var(--line); border-radius:12px; width:min(680px,94vw); max-height:88vh; overflow:auto; padding:18px 20px; box-shadow:var(--shadow); }
  .modal-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:4px; }
  .modal-head h2 { margin:0; font-size:16px; }
  .modal .x { background:transparent; color:var(--dim); border:0; font-size:18px; padding:2px 8px; }
  .rule { display:grid; grid-template-columns:1fr 92px 92px 96px 32px; gap:8px; align-items:center; margin-bottom:7px; }
  .rule input, .rule select { padding:6px 8px; }
  .rule .delrule { background:transparent; border:1px solid var(--line); color:var(--ev-exit); padding:5px 8px; }
  .modal-foot { display:flex; align-items:center; gap:12px; margin-top:14px; }
  .modal-foot button:not(.ghost) { margin-left:auto; }
  .rule-head { display:grid; grid-template-columns:1fr 92px 92px 96px 32px; gap:8px; font-size:10.5px; color:var(--faint); text-transform:uppercase; letter-spacing:.06em; margin:8px 0 4px; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="dot" aria-hidden="true"></span>
    <h1>Viewer Event CCTV</h1>
    <span class="sub">rumah · kamera depan</span>
  </div>
  <div class="spacer"></div>
  <button class="hbtn" id="openCfg">⚙ Jadwal</button>
  <div class="kpis mono">
    <div class="kpi"><b id="kTotal">0</b><span class="eyebrow">event</span></div>
    <div class="kpi"><b id="kShown">0</b><span class="eyebrow">tampil</span></div>
    <div class="kpi"><b id="kClips">0</b><span class="eyebrow">klip</span></div>
  </div>
</header>

<div class="console">
  <div class="left">
    <div class="filters">
      <div class="seg" id="modeSeg" role="tablist">
        <button data-m="event" aria-selected="true">Event</button>
        <button data-m="episode" aria-selected="false">Episode</button>
        <button data-m="arsip" aria-selected="false">Arsip</button>
      </div>
      <div class="search">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="q" placeholder="cari zona, jenis, jam…" autocomplete="off" autofocus>
      </div>
      <div class="chips eventOnly" id="kindChips">
        <button class="chip k-dwell"  data-k="close"   aria-pressed="false">berdiam (close)</button>
        <button class="chip k-loiter" data-k="loiter"  aria-pressed="false">berlama (loiter)</button>
        <button class="chip k-enter"  data-k="MASUK"   aria-pressed="false">masuk</button>
        <button class="chip k-exit"   data-k="KELUAR"  aria-pressed="false">keluar</button>
      </div>
      <div class="frow">
        <select id="zone" class="eventOnly" aria-label="Zona"><option value="">semua zona</option></select>
        <select id="day" aria-label="Hari"><option value="">semua hari</option></select>
      </div>
      <label class="note eventOnly"><input type="checkbox" id="mediaOnly"> hanya yang punya video/snapshot</label>
      <label class="note episodeOnly" hidden>jeda maksimal antar-passage
        <input id="gap" class="num" type="number" value="20" min="1"> dtk</label>
    </div>
    <div class="toolbar">
      <button id="playAll">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        Putar semua
      </button>
      <button class="ghost" id="clear">Reset</button>
      <span class="count mono" id="count">0 hasil</span>
    </div>
    <div class="log">
      <table>
        <thead><tr><th>Waktu</th><th>Jenis</th><th>Zona</th><th>Durasi</th><th></th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="placeholder" id="noRows" hidden>tak ada event yang cocok</div>
    </div>
  </div>

  <div class="right">
    <div class="stage" id="stage"><div class="placeholder">pilih sebuah event untuk memutar rekamannya</div></div>
    <div id="detailWrap"></div>
    <div id="nvrWrap"></div>
    <div class="queue" id="queueWrap" hidden>
      <div class="qhead"><span class="eyebrow">Antrean putar-semua</span><span class="note mono" id="qmeta"></span></div>
      <ol id="queue"></ol>
    </div>
  </div>
</div>

<div class="modal" id="cfgModal" hidden>
  <div class="modal-box">
    <div class="modal-head"><h2>⚙ Jadwal notifikasi</h2><button class="x" id="cfgClose">✕</button></div>
    <p class="note"><b>senyap</b> = pipeline TETAP merekam + tulis log, hanya notifikasi Telegram dibungkam.
       Berlaku live tanpa restart. Kosongkan zona = berlaku untuk semua.</p>
    <div class="frow" style="max-width:260px"><label class="note">Default</label>
      <select id="cfgDefault"><option value="aktif">aktif</option><option value="senyap">senyap</option></select></div>
    <div class="rule-head"><span>zona / gerbang (koma; kosong=semua)</span><span>dari</span><span>sampai</span><span>notif</span><span></span></div>
    <div id="cfgRules"></div>
    <button class="ghost" id="cfgAdd" style="margin-top:4px">+ tambah aturan</button>
    <div class="modal-foot"><span class="note" id="cfgStatus"></span><button id="cfgSave">Simpan</button></div>
  </div>
</div>

<script>
"use strict";
const $ = s => document.querySelector(s);
let ALL = [], selId = null, activeKinds = new Set(), queue = [], qOn = -1, META = {}, SELE = null, MODE = "event";

const kindClass = k => k==="close" ? "k-dwell" : k==="loiter" ? "k-loiter"
  : k.startsWith("MASUK") ? "k-enter" : k.startsWith("KELUAR") ? "k-exit" : "k-plain";
const kindIcon = m => !m ? "" : (m.type==="video" ? "🎬" : "🖼");
const family = k => k==="close" ? "close" : k==="loiter" ? "loiter" : k.split(" ")[0];
const dirClass = d => d==="keluar" ? "k-exit" : "k-enter";

async function loadMeta(){
  const m = await (await fetch("/api/meta")).json();
  META = m;
  const zsel = $("#zone"); m.zones.forEach(z => zsel.add(new Option(z, z)));
  const dsel = $("#day");  m.days.forEach(d => dsel.add(new Option(d, d)));
  $("#kTotal").textContent = m.live;      // event live (yang punya jam nyata)
  $("#kClips").textContent = m.clips;
  if(!m.segrec){ const b = $('#modeSeg [data-m="arsip"]'); if(b) b.hidden = true; }
}

function serverParams(){
  const p = new URLSearchParams();
  if($("#q").value.trim()) p.set("q", $("#q").value.trim());
  if($("#zone").value) p.set("zone", $("#zone").value);
  if($("#day").value){ p.set("from", $("#day").value); p.set("to", $("#day").value); }
  p.set("live", "1");
  if($("#mediaOnly").checked) p.set("media", "1");
  return p.toString();
}

async function fetchEvents(){
  ALL = await (await fetch("/api/events?" + serverParams())).json();
  render();
}

function visible(){
  if(MODE==="episode"){
    const q = $("#q").value.trim().toLowerCase(), day = $("#day").value;
    return ALL.filter(e => {
      if(day && e.when.slice(0,10) !== day) return false;
      if(q){ const hay = `${e.dir} ${e.gates.join(" ")} ${e.when}`.toLowerCase(); if(!hay.includes(q)) return false; }
      return true;
    });
  }
  if(MODE==="arsip"){
    const q = $("#q").value.trim().toLowerCase(), day = $("#day").value;
    return ALL.filter(e => {
      if(day && e.when.slice(0,10) !== day) return false;
      if(q && !e.when.toLowerCase().includes(q)) return false;
      return true;
    });
  }
  if(!activeKinds.size) return ALL;
  return ALL.filter(e => activeKinds.has(family(e.kind)));
}

function evRow(e){
  return `<td class="mono">${e.when.slice(11)}<span class="med" style="margin-left:6px">${e.when.slice(5,10)}</span></td>
    <td><span class="tag ${kindClass(e.kind)}">${e.kind}</span></td>
    <td><span class="zone">${e.zone||"—"}</span>${e.species==="kucing"?' <span class="cat">🐈 kucing</span>':''}</td>
    <td class="mono dur">${e.dur!=null? e.dur.toFixed(1)+"s":"—"}</td>
    <td class="med">${kindIcon(e.media)}</td>`;
}
function epRow(e){
  return `<td class="mono">${e.when.slice(11)}<span class="med" style="margin-left:6px">${e.when.slice(5,10)}</span></td>
    <td><span class="tag ${dirClass(e.dir)}">${e.dir}</span></td>
    <td>${e.gates.join(" + ")}</td>
    <td class="mono dur">${e.dur.toFixed(1)}s</td>
    <td class="med">${e.media?"🎬 ":""}<span class="mono">×${e.count}</span></td>`;
}
function segRow(e){
  return `<td class="mono">${e.when.slice(0,10)}</td>
    <td class="mono">${e.hour}:00</td>
    <td class="mono med">${e.n} segmen</td>
    <td></td><td class="med">🎞</td>`;
}

function render(){
  const list = visible();
  const tb = $("#rows"); tb.innerHTML = "";
  $("#noRows").hidden = list.length > 0;
  $("#count").textContent = list.length + (MODE==="episode" ? " episode" : MODE==="arsip" ? " jam" : " hasil");
  $("#kShown").textContent = list.length;
  for(const e of list){
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    tr.setAttribute("aria-selected", e.id===selId ? "true" : "false");
    tr.innerHTML = MODE==="episode" ? epRow(e) : MODE==="arsip" ? segRow(e) : evRow(e);
    const pick = () => select(e.id);
    tr.onclick = pick;
    tr.onkeydown = ev => { if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); pick(); } };
    tb.appendChild(tr);
  }
}

function byId(id){ return ALL.find(e => e.id===id); }

function select(id, playlistMode){
  selId = id;
  document.querySelectorAll('tbody tr[aria-selected="true"]').forEach(tr=>tr.setAttribute("aria-selected","false"));
  const list = visible();
  const idx = list.findIndex(e=>e.id===id);
  const trs = $("#rows").children;
  if(idx>=0 && trs[idx]){ trs[idx].setAttribute("aria-selected","true"); trs[idx].scrollIntoView({block:"nearest"}); }
  const e = byId(id);
  if(MODE==="arsip"){
    SELE = e; $("#nvrWrap").innerHTML = "";
    renderSegStage(e);
  } else if(MODE==="episode"){
    SELE = Object.assign({}, e, {lo: e.start, hi: e.end});   // lo/hi -> renderNvr membentang episode
    renderEpStage(e); renderEpDetail(e); renderNvr(SELE);
  } else {
    SELE = e;
    renderStage(e, playlistMode); renderDetail(e); renderNvr(e);
  }
}

function renderEpStage(e){
  if(e.media){    // klip transit tersimpan -> putar langsung
    $("#stage").innerHTML = `
      <div class="screen">
        <video id="vid" controls autoplay playsinline src="${e.media.url}"></video>
        <div class="scan"></div>
        <div class="rec"><span class="b"></span>${e.dir.toUpperCase()}</div>
        <div class="osd-tr mono">${e.when} · ${e.dur.toFixed(1)}s</div>
      </div>
      <div class="cap"><span class="fn mono">${e.media.file}</span>
        <a href="${e.media.url}?download=1">⬇ unduh klip transit</a></div>`;
  } else {
    $("#stage").innerHTML = `<div class="placeholder">
      Episode <span class="tag ${dirClass(e.dir)}">${e.dir}</span> · ${e.gates.join(" + ")} · ${e.dur.toFixed(1)}s<br>
      <span class="note">belum ada klip transit tersimpan — tarik footage penuh dari NVR di bawah</span></div>`;
  }
}
function renderEpDetail(e){
  $("#detailWrap").innerHTML = `<div class="detail">
    <h2><span class="tag ${dirClass(e.dir)}">${e.dir}</span> <span>${e.gates.join(" + ")}</span></h2>
    <dl class="kv">
      <dt>Mulai</dt><dd class="mono">${e.when}</dd>
      <dt>Span</dt><dd>${e.dur.toFixed(1)} detik</dd>
      <dt>Passage</dt><dd>${e.kinds.join(" → ")}</dd>
    </dl></div>`;
}

// ── tarik footage penuh dari NVR ──
function renderNvr(e){
  const w = $("#nvrWrap");
  if(!META.nvr){ w.innerHTML = ""; return; }   // NVR belum dikonfigurasi (.env)
  w.innerHTML = `<div class="detail nvr">
    <div class="qhead"><span class="eyebrow">Footage penuh dari NVR</span></div>
    <div class="note">Tarik rekaman utuh rentang event ini dari NVR (mainstream full-res) — untuk develop / contoh.</div>
    <div class="nvrctl">
      <label>sebelum <input id="padPre" class="num" type="number" value="5" min="0"> dtk</label>
      <label>sesudah <input id="padPost" class="num" type="number" value="5" min="0"> dtk</label>
      <span class="note mono" id="nvrRange"></span>
    </div>
    <div class="nvract"><button id="nvrGrab">⬇ Ambil dari NVR</button><span class="note" id="nvrStatus"></span></div>
    <div id="nvrResult"></div></div>`;
  $("#padPre").oninput = updateRange;
  $("#padPost").oninput = updateRange;
  $("#nvrGrab").onclick = grabNvr;
  updateRange();
}

function nvrRange(){
  const pre = +$("#padPre").value || 0, post = +$("#padPost").value || 0;
  return { start: SELE.lo - pre, end: SELE.hi + post };
}
function updateRange(){
  const {start, end} = nvrRange();
  const fmt = ep => new Date(ep*1000).toLocaleTimeString("id-ID", {hour12:false});
  $("#nvrRange").textContent = `${fmt(start)} – ${fmt(end)}  (${(end-start).toFixed(1)}s)`;
}
function playUrl(url, file, tag){
  killHls();
  $("#stage").innerHTML = `
    <div class="screen"><video controls autoplay playsinline src="${url}"></video>
      <div class="scan"></div><div class="rec"><span class="b"></span>${tag||"NVR"}</div></div>
    <div class="cap"><span class="fn mono">${file}</span><a href="${url}?download=1">⬇ unduh</a></div>`;
}
async function grabNvr(){
  if(!SELE) return;
  const {start, end} = nvrRange();
  const btn = $("#nvrGrab"), st = $("#nvrStatus");
  btn.disabled = true;
  st.innerHTML = '<span class="spinner"></span> menarik dari NVR…';
  try {
    const r = await (await fetch(`/api/nvr/grab?start=${start}&end=${end}`)).json();
    if(r.ok){
      st.textContent = r.cached ? "sudah ada (cache)" : `selesai · ${((r.size||0)/1e6).toFixed(1)} MB`;
      $("#nvrResult").innerHTML = `<div class="done">
        <button class="playnvr" data-u="${r.url}" data-f="${r.file}">▶ putar hasil</button>
        <a class="dl" href="${r.url}?download=1">⬇ unduh (${r.seconds}s)</a>
        <span class="mono med">${r.file}</span></div>`;
      $("#nvrResult .playnvr").onclick = ev => playUrl(ev.currentTarget.dataset.u, ev.currentTarget.dataset.f);
      playUrl(r.url, r.file);
    } else {
      st.textContent = "gagal: " + r.error;
    }
  } catch(err){ st.textContent = "error: " + err; }
  btn.disabled = false;
}

// ── arsip segrec: potong rentang waktu dari segmen ──
async function fetchSegIndex(){
  const r = await (await fetch("/api/seg/index")).json();
  ALL = [];
  (r.days||[]).forEach(d => (d.hours||[]).forEach(h => {
    ALL.push({ id: ALL.length, day: d.day, hour: h.h, n: h.n,
      when: `${d.day.slice(0,4)}-${d.day.slice(4,6)}-${d.day.slice(6,8)} ${h.h}:00` });
  }));
  render();
}
function renderSegStage(e){
  $("#stage").innerHTML = `<div class="placeholder">Pilih rentang waktu di bawah, lalu <b>Putar rentang</b>.</div>`;
  $("#detailWrap").innerHTML = `<div class="detail nvr">
    <div class="qhead"><span class="eyebrow">Arsip ${e.when.slice(0,10)} · jam ${e.hour}:00 · ${e.n} segmen</span></div>
    <div class="note">Potong rekaman utuh dari arsip untuk rentang pilihan (maks ${META.segMax||300}s). Lintas-jam boleh.</div>
    <div class="nvrctl">
      <label>dari <input id="segStart" type="time" step="1" value="${e.hour}:00:00" style="width:118px"></label>
      <label>sampai <input id="segEnd" type="time" step="1" value="${e.hour}:00:30" style="width:118px"></label>
      <span class="note mono" id="segRange"></span>
    </div>
    <div class="nvract"><button id="segHls">▶ Putar 1 jam (HLS)</button>
      <button id="segPlay" class="ghost">✂ potong rentang</button>
      <span class="note" id="segStatus"></span></div>
    <div id="segResult"></div></div>`;
  $("#segStart").oninput = updateSegRange;
  $("#segEnd").oninput = updateSegRange;
  $("#segHls").onclick = () => playHls(SELE.day, SELE.hour);
  $("#segPlay").onclick = grabSeg;
  updateSegRange();
}
let _hlsLib = null, curHls = null;
function ensureHls(){
  if(_hlsLib) return _hlsLib;
  _hlsLib = new Promise((res, rej) => {
    if(window.Hls){ res(); return; }
    const s = document.createElement("script");
    s.src = "/static/hls.light.min.js";
    s.onload = () => res(); s.onerror = () => rej(new Error("gagal muat hls.js"));
    document.head.appendChild(s);
  });
  return _hlsLib;
}
function killHls(){ if(curHls){ try{ curHls.destroy(); }catch(e){} curHls = null; } }
async function playHls(day, hour){
  killHls();
  const url = `/api/seg/hls?day=${day}&hour=${hour}`;
  $("#stage").innerHTML = `
    <div class="screen"><video id="hlsvid" controls autoplay playsinline></video>
      <div class="scan"></div><div class="rec"><span class="b"></span>ARSIP ${hour}:00</div></div>
    <div class="cap"><span class="fn mono">jam ${hour}:00 · HLS (jam penuh, seek instan)</span>
      <a href="${url}">playlist .m3u8</a></div>`;
  const v = $("#hlsvid");
  // hls.js DULU: Chrome balas canPlayType('...mpegurl')='maybe' padahal tak bisa
  // putar HLS native -> jangan percaya; native hanya untuk Safari (isSupported=false).
  try {
    await ensureHls();
    if(window.Hls && Hls.isSupported()){
      curHls = new Hls({ maxBufferLength: 30 });
      curHls.on(Hls.Events.ERROR, (_, d) => { if(d.fatal) $("#segStatus").textContent = "HLS error: " + d.details; });
      curHls.loadSource(url); curHls.attachMedia(v);
      return;
    }
  } catch(err){ /* jatuh ke native di bawah */ }
  if(v.canPlayType("application/vnd.apple.mpegurl")){ v.src = url; }          // Safari native
  else { $("#stage").innerHTML = `<div class="placeholder">HLS tak didukung browser ini.</div>`; }
}
function _hms(t){ const p=(t||"").split(":").map(Number); return (p[0]||0)*3600+(p[1]||0)*60+(p[2]||0); }
function updateSegRange(){
  const d = _hms($("#segEnd").value) - _hms($("#segStart").value);
  $("#segRange").textContent = `${d} dtk`;
}
async function grabSeg(){
  if(!SELE) return;
  const day = SELE.day, start = $("#segStart").value, end = $("#segEnd").value;
  const btn = $("#segPlay"), st = $("#segStatus");
  btn.disabled = true;
  st.innerHTML = '<span class="spinner"></span> memotong dari arsip…';
  try {
    const r = await (await fetch(`/api/seg/clip?day=${day}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`)).json();
    if(r.ok){
      st.textContent = r.cached ? "sudah ada (cache)" : `selesai · ${((r.size||0)/1e6).toFixed(1)} MB`;
      $("#segResult").innerHTML = `<div class="done">
        <button class="playseg" data-u="${r.url}" data-f="${r.file}">▶ putar hasil</button>
        <a class="dl" href="${r.url}?download=1">⬇ unduh (${r.seconds}s)</a>
        <span class="mono med">${r.file}</span></div>`;
      $("#segResult .playseg").onclick = ev => playUrl(ev.currentTarget.dataset.u, ev.currentTarget.dataset.f, "ARSIP");
      playUrl(r.url, r.file, "ARSIP");
    } else {
      st.textContent = "gagal: " + r.error;
    }
  } catch(err){ st.textContent = "error: " + err; }
  btn.disabled = false;
}

function renderStage(e, playlistMode){
  const stage = $("#stage");
  if(!e.media){ stage.innerHTML = '<div class="placeholder">event ini tak punya video/snapshot</div>'; return; }
  if(e.media.type==="video"){
    stage.innerHTML = `
      <div class="screen">
        <video id="vid" controls autoplay playsinline></video>
        <div class="scan"></div>
        <div class="rec"><span class="b"></span>REC</div>
        <div class="osd-tr mono">${e.when}${e.dur!=null? " · "+e.dur.toFixed(1)+"s":""}</div>
      </div>
      <div class="cap"><span class="fn mono">${e.media.file}</span>
        <a href="${e.media.url}?download=1">⬇ unduh klip</a></div>`;
    const v = $("#vid");
    v.src = e.media.url;
    if(playlistMode) v.onended = playNext;
  } else {
    stage.innerHTML = `
      <div class="screen">
        <img src="${e.media.url}" alt="snapshot ${e.kind}">
        <div class="scan"></div>
        <div class="rec"><span class="b"></span>SNAPSHOT</div>
        <div class="osd-tr mono">${e.when}</div>
      </div>
      <div class="cap"><span class="fn mono">${e.media.file}</span>
        <a href="${e.media.url}?download=1">⬇ unduh snapshot</a></div>`;
  }
}

function renderDetail(e){
  $("#detailWrap").innerHTML = `
    <div class="detail">
      <h2><span class="tag ${kindClass(e.kind)}">${e.kind}</span> <span>${e.zone||""}</span></h2>
      <dl class="kv">
        <dt>Waktu</dt><dd class="mono">${e.when}</dd>
        <dt>Zona</dt><dd>${e.zone||"—"}</dd>
        <dt>Durasi</dt><dd>${e.dur!=null? e.dur.toFixed(1)+" detik":"—"}</dd>
        <dt>Subjek</dt><dd>${e.species==="kucing"?"🐈 kucing":"orang"}</dd>
        <dt>Berkas</dt><dd class="mono med">${e.media? e.media.file : "— (tak ada)"}</dd>
      </dl>
    </div>`;
}

// ── putar semua (stream berurutan) ──
function playAll(){
  queue = visible().filter(e => e.media && e.media.type==="video");
  const wrap = $("#queueWrap"), ol = $("#queue");
  wrap.hidden = false;
  if(!queue.length){ ol.innerHTML = '<div class="empty">tak ada klip video di hasil ini</div>'; $("#qmeta").textContent=""; return; }
  ol.innerHTML = queue.map((e,i)=>`<li data-i="${i}"><span class="n mono">${i+1}</span>
     <span class="tag ${kindClass(e.kind)}">${e.kind}</span>
     <span class="zone">${e.zone||""}</span>
     <span class="mono med" style="margin-left:auto">${e.when.slice(11)}</span></li>`).join("");
  ol.querySelectorAll("li").forEach(li => li.onclick = () => advance(+li.dataset.i));
  advance(0);
}
function playNext(){ if(qOn+1 < queue.length) advance(qOn+1); else $("#qmeta").textContent = "— selesai —"; }
function advance(i){
  qOn = i;
  $("#qmeta").textContent = `${i+1} / ${queue.length}`;
  $("#queue").querySelectorAll("li").forEach((li,j)=> li.classList.toggle("on", j===i));
  select(queue[i].id, true);
}

// ── mode & fetch ──
async function fetchEpisodes(){
  const gap = +($("#gap").value || 20);
  ALL = await (await fetch("/api/episodes?gap=" + gap)).json();
  render();
}
function fetchData(){ return MODE==="episode" ? fetchEpisodes() : MODE==="arsip" ? fetchSegIndex() : fetchEvents(); }
function refresh(){ return MODE==="event" ? fetchEvents() : render(); }

function setMode(m){
  if(m === MODE) return;
  MODE = m;
  $("#modeSeg").querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", b.dataset.m===m ? "true" : "false"));
  document.querySelectorAll(".eventOnly").forEach(x => x.hidden = m!=="event");
  document.querySelectorAll(".episodeOnly").forEach(x => x.hidden = m!=="episode");
  const ths = document.querySelectorAll("thead th");
  ths[0].textContent = m==="arsip" ? "Tanggal" : "Waktu";
  ths[1].textContent = m==="episode" ? "Arah" : m==="arsip" ? "Jam" : "Jenis";
  ths[2].textContent = m==="episode" ? "Gerbang" : m==="arsip" ? "Segmen" : "Zona";
  ths[3].textContent = m==="arsip" ? "" : "Durasi";
  ths[4].textContent = m==="episode" ? "#" : "";
  $("#playAll").hidden = m!=="event";
  selId = null; SELE = null; killHls();
  $("#detailWrap").innerHTML = ""; $("#nvrWrap").innerHTML = "";
  const label = m==="episode" ? "episode" : m==="arsip" ? "jam arsip" : "event";
  $("#stage").innerHTML = `<div class="placeholder">pilih sebuah ${label}…</div>`;
  $("#queueWrap").hidden = true;
  fetchData();
}

// ── wire ──
let t;
$("#q").oninput = () => { clearTimeout(t); t = setTimeout(refresh, 200); };
$("#day").onchange = refresh;
["zone","mediaOnly"].forEach(id => $("#"+id).onchange = fetchEvents);
$("#gap").oninput = () => { clearTimeout(t); t = setTimeout(fetchEpisodes, 250); };
$("#modeSeg").addEventListener("click", ev => { const b = ev.target.closest("button"); if(b) setMode(b.dataset.m); });
$("#kindChips").addEventListener("click", ev => {
  const b = ev.target.closest(".chip"); if(!b) return;
  const k = b.dataset.k, on = b.getAttribute("aria-pressed")==="true";
  b.setAttribute("aria-pressed", on ? "false" : "true");
  on ? activeKinds.delete(k) : activeKinds.add(k);
  render();
});
$("#clear").onclick = () => {
  $("#q").value=""; $("#zone").value=""; $("#day").value=""; $("#mediaOnly").checked=false;
  activeKinds.clear();
  document.querySelectorAll(".chip").forEach(c => c.setAttribute("aria-pressed","false"));
  $("#queueWrap").hidden = true;
  fetchData();
};
$("#playAll").onclick = playAll;

// ── jadwal arming (config) ──
function ruleRow(r={}){
  const div = document.createElement("div"); div.className = "rule";
  div.innerHTML = `
    <input class="zones" placeholder="mis. teras, pintu (kosong=semua)" value="${(r.zones||[]).join(", ")}">
    <input class="from" type="time" value="${r.from||"22:00"}">
    <input class="to" type="time" value="${r.to||"06:00"}">
    <select class="notif">
      <option value="aktif"${r.notif!=="senyap"?" selected":""}>aktif</option>
      <option value="senyap"${r.notif==="senyap"?" selected":""}>senyap</option></select>
    <button class="delrule" title="hapus">✕</button>`;
  div.querySelector(".delrule").onclick = () => div.remove();
  return div;
}
async function openCfg(){
  const cfg = await (await fetch("/api/arming")).json();
  $("#cfgDefault").value = cfg.default || "aktif";
  const box = $("#cfgRules"); box.innerHTML = "";
  (cfg.rules||[]).forEach(r => box.appendChild(ruleRow(r)));
  $("#cfgStatus").textContent = "";
  $("#cfgModal").hidden = false;
}
function collectCfg(){
  const rules = [...$("#cfgRules").children].map(div => {
    const zones = div.querySelector(".zones").value.split(",").map(s=>s.trim()).filter(Boolean);
    const r = { from: div.querySelector(".from").value || "00:00",
                to: div.querySelector(".to").value || "24:00",
                notif: div.querySelector(".notif").value };
    if(zones.length) r.zones = zones;
    return r;
  });
  return { default: $("#cfgDefault").value, rules };
}
async function saveCfg(){
  const st = $("#cfgStatus"); st.textContent = "menyimpan…";
  try {
    const r = await (await fetch("/api/arming", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(collectCfg())})).json();
    st.textContent = r.ok ? "tersimpan ✓ — berlaku live (tanpa restart)" : "gagal: " + r.error;
  } catch(e){ st.textContent = "error: " + e; }
}
$("#openCfg").onclick = openCfg;
$("#cfgClose").onclick = () => $("#cfgModal").hidden = true;
$("#cfgModal").onclick = ev => { if(ev.target === $("#cfgModal")) $("#cfgModal").hidden = true; };
$("#cfgAdd").onclick = () => $("#cfgRules").appendChild(ruleRow());
$("#cfgSave").onclick = saveCfg;

loadMeta().then(fetchData);
</script>
</body>
</html>"""


# ══ jalankan ═══════════════════════════════════════════════════════════════════
def start_server(host, preferred_port):
    """Coba port pilihan; kalau sibuk, minta OS beri port bebas. Kembalikan (srv, port)."""
    try:
        srv = ThreadingHTTPServer((host, preferred_port), Handler)
    except OSError:
        srv = ThreadingHTTPServer((host, 0), Handler)   # 0 = OS pilih port bebas
    return srv, srv.server_address[1]


APP_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "brave-browser", "microsoft-edge")


def find_app_browser():
    for b in APP_BROWSERS:
        path = shutil.which(b)
        if path:
            return path
    return None


def main():
    global ROOT, EVENTS_PATH, MEDIA_DIR, ARMING_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="folder berisi events-live.jsonl & out/live")
    ap.add_argument("--events", default="events-live.jsonl")
    ap.add_argument("--media-dir", default="out/live")
    ap.add_argument("--arming-file", default="arming.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default=8477, type=int, help="port pilihan (kalau sibuk, dipilih otomatis)")
    ap.add_argument("--no-open", action="store_true",
                    help="mode server saja: jangan buka jendela, biarkan jalan (untuk bookmark/remote)")
    ap.add_argument("--tab", action="store_true",
                    help="buka sebagai tab browser biasa, bukan jendela-app Chromium")
    ap.add_argument("--nvr-track", default="201",
                    help="track Hikvision utk playback penuh (ch2/taman=201, ch1/garasi=101)")
    ap.add_argument("--nvr-dir", default="out/nvr", help="folder simpan hasil tarikan NVR")
    ap.add_argument("--nvr-max-seconds", default=600, type=int, help="batas durasi satu tarikan NVR")
    ap.add_argument("--seg-dir", default="out/segments", help="arsip segmen segrec (telusur per waktu)")
    ap.add_argument("--seg-clip-dir", default="out/segclip", help="folder simpan hasil potong arsip")
    ap.add_argument("--seg-max-seconds", default=300, type=int, help="batas durasi satu potongan arsip")
    args = ap.parse_args()

    ROOT = args.root
    EVENTS_PATH = args.events
    MEDIA_DIR = args.media_dir
    ARMING_FILE = args.arming_file
    load_nvr(ROOT, args.nvr_track, args.nvr_dir, args.nvr_max_seconds)
    load_segrec(ROOT, args.seg_dir, args.seg_clip_dir, args.seg_max_seconds)

    srv, port = start_server(args.host, args.port)
    url = f"http://{args.host}:{port}"
    print(f"Viewer CCTV → {url}   (root={os.path.abspath(ROOT)})")

    # server di thread; main menunggu (jendela-app / Ctrl-C)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    browser = None if (args.no_open or args.tab) else find_app_browser()

    if browser:
        # jendela-app Chromium: terasa seperti aplikasi terpisah (tanpa bilah tab).
        # server MATI saat jendela ditutup -> siklus hidup seperti app.
        print("Membuka jendela app… (tutup jendela = berhenti)")
        profile = os.path.join(os.path.expanduser("~"), ".cache", "cctv-viewer-app")
        proc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}",
                                 "--no-first-run", "--no-default-browser-check"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        srv.shutdown()
        print("berhenti.")
    else:
        if not args.no_open:
            threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True).start()
        print("Ctrl-C untuk berhenti.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nberhenti.")
            srv.shutdown()


if __name__ == "__main__":
    main()
