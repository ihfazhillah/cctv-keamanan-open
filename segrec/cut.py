"""Potong klip A/V dari segmen bergulir untuk rentang waktu [t0,t1] (epoch).

Dipakai bot saat mengirim: pilih segmen yang menutupi jendela event, sambung
`-c copy` (segmen sudah h264+AAC ber-timeline wallclock -> durasi benar, tanpa
re-encode) -> MP4 beraudio & mulus. Kembalikan path, atau None bila tak ada
segmen menutupi (mis. event lebih lama dari retensi / segrec mati)."""
import os
import re
import glob
import time
import subprocess

SEG_RE = re.compile(r"seg_(\d{8}_\d{6})\.ts$")


def seg_epoch(path):
    m = SEG_RE.search(path)
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1), "%Y%m%d_%H%M%S"))


def _hour_dirs(seg_dir, t0, t1):
    """Subfolder YYYYMMDD/HH yang mungkin memuat [t0,t1] (+padding 1 jam)."""
    dirs, seen, t = [], set(), t0 - 3600
    while t <= t1 + 3600:
        lt = time.localtime(t)
        d = os.path.join(seg_dir, time.strftime("%Y%m%d", lt), time.strftime("%H", lt))
        if d not in seen:
            seen.add(d)
            dirs.append(d)
        t += 1800
    return dirs


def pilih_segmen(seg_dir, t0, t1, seg_time=4):
    """Segmen yang beririsan dgn [t0,t1] (nama = waktu MULAI segmen). Cari di
    subfolder per-jam yang relevan (skala besar) + root (legacy flat)."""
    kandidat = []
    for d in _hour_dirs(seg_dir, t0, t1):
        if os.path.isdir(d):
            kandidat.extend(glob.glob(os.path.join(d, "seg_*.ts")))
    kandidat.extend(glob.glob(os.path.join(seg_dir, "seg_*.ts")))    # legacy flat
    out = []
    for p in kandidat:
        s = seg_epoch(p)
        if s is not None and s < t1 and (s + seg_time) > t0:
            out.append(p)
    return sorted(set(out))


def cut(seg_dir, t0, t1, out_path, seg_time=4):
    pilih = pilih_segmen(seg_dir, t0, t1, seg_time)
    if not pilih:
        return None
    lst = out_path + ".txt"
    with open(lst, "w") as f:
        for p in pilih:
            f.write(f"file '{os.path.abspath(p)}'\n")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", "-movflags", "+faststart", out_path],
            capture_output=True, text=True, timeout=90)
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None
    return out_path


def window_for(payload, pre=4.0, post=6.0):
    """Jendela [t0,t1] untuk event, dari payload (epoch)."""
    kind = payload.get("kind")
    if kind == "close":
        return payload.get("start", 0) - pre, payload.get("end", payload.get("start", 0)) + post
    if kind == "loiter":
        return payload.get("start", 0) - pre, payload.get("at", payload.get("start", 0)) + post
    st = payload.get("start", payload.get("at", 0))     # transit masuk/keluar = momen
    return st - pre, st + post
