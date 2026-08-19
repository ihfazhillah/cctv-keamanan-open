"""Coba MODEL & RULE dari klip yang sudah ada — tanpa live, tanpa app buatan.

Alur (lihat artifact "Model ⊥ Rule"): klip --[trace: kerjaan MODEL]--> observasi
--[replay: kerjaan RULE]--> event. `trace()` jalankan YOLO SEKALI lalu di-cache;
`replay_*()` cuma membungkus KELAS RULE ASLI (Tripwire/FootTracker/SceneNotifier) —
tak menduplikasi logika. Helper visual menumpang penonton umum (image viewer/VLC/
matplotlib), bukan GUI buatan.

    from harness import trace, replay_tripwire, replay_scene, overlay_video, contact_sheet, plot_signals

    tr = trace("out/segments/garasi/20260819/03/seg_20260819_031045.ts",
               model="yolo11s.pt", conf=0.20)                     # cached
    cross = replay_tripwire(tr, [{"nama":"asrama","garis":[[0.559,0.461],[0.648,0.372]]}])
    overlay_video("out/segments/.../seg_..031045.ts", tr, "/tmp/ov.mp4",
                  lines=[{"nama":"asrama","garis":[[0.559,0.461],[0.648,0.372]]}], events=cross)

CLI cepat:  uv run pipeline/harness.py <klip.ts> [--lines x1,y1,x2,y2 ...]

Catatan .ts: dibaca via cv2 VideoCapture BERURUTAN (ffmpeg -ss / seek ke .ts tunggal
sering gagal/meleset — segmen tanpa indeks keyframe rapat).
"""
import os
import json
import glob
import hashlib
import argparse

import cv2
import numpy as np

# KELAS RULE ASLI (tak menduplikasi logika) — inilah yang di-'replay'
from rules import (Tripwire, FootTracker, _garis_lines, LUAR,  # noqa: F401
                   replay_tripwire, replay_scene)

TRACE_DIR = "out/traces"
# warna BGR (cv2): model=amber, rule=teal, kaki=putih, event=merah
C_BOX, C_LINE, C_FOOT, C_EVENT = (40, 150, 220), (150, 180, 40), (235, 235, 235), (60, 60, 235)


# ══ PERSEPSI: klip -> observasi (di-cache) ══════════════════════════════════════
def frames(clip, step=1):
    """(t_detik_relatif, frame BGR) berurutan dari .ts/.mp4. step=N -> tiap ke-N."""
    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            yield round(i / fps, 3), fr
        i += 1
    cap.release()


def _load_zones(zone_file):
    data = json.loads(open(zone_file).read())
    return {n: np.array(p, np.int32) for n, p in data.get("zones", {}).items()}


def _zona_of(foot_px, zones):
    for n, poly in zones.items():
        if cv2.pointPolygonTest(poly, (float(foot_px[0]), float(foot_px[1])), False) >= 0:
            return n
    return None


def _cache_key(clip, model, conf, classes, tracker, zone_file, step):
    try:
        mt = os.path.getmtime(clip)
    except OSError:
        mt = 0
    raw = f"{clip}|{mt}|{model}|{conf}|{classes}|{tracker}|{zone_file}|{step}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def trace(clip, model="yolo11s.pt", conf=0.20, classes=(0,), tracker=None,
          zone_file=None, step=1, cache=True):
    """klip -> list observasi per-frame (KERJAAN MODEL). Baris:
        {"t", "wh":[w,h], "count", "dets":[{tid, xyxy(fraksi), kaki(fraksi), conf, cls, zona}]}
    YOLO dijalankan SEKALI lalu disimpan `out/traces/<hash>.json` (ganti model/conf/tracker
    = trace baru). tracker=None -> predict (tid None; pakai FootTracker saat replay); beri
    'botsort_reid.yaml' untuk track ber-ID. count butuh zone_file (orang di zona non-LUAR)."""
    key = _cache_key(clip, model, conf, tuple(classes), tracker, zone_file, step)
    cp = os.path.join(TRACE_DIR, key + ".json")
    if cache and os.path.exists(cp):
        return json.load(open(cp))["frames"]

    from ultralytics import YOLO                          # berat -> impor lazy
    m = YOLO(model)
    zones = _load_zones(zone_file) if zone_file else {}
    out = []
    for t, fr in frames(clip, step):
        h, w = fr.shape[:2]
        if tracker:
            r = m.track(fr, persist=True, tracker=tracker, classes=list(classes), conf=conf, verbose=False)[0]
            ids = r.boxes.id.tolist() if (r.boxes is not None and r.boxes.id is not None) else None
        else:
            r = m.predict(fr, classes=list(classes), conf=conf, verbose=False)[0]
            ids = None
        dets, b = [], r.boxes
        if b is not None and len(b):
            xy, cf, cl = b.xyxy.tolist(), b.conf.tolist(), b.cls.tolist()
            for k in range(len(b)):
                x1, y1, x2, y2 = xy[k]
                foot_px = ((x1 + x2) / 2, y2)
                dets.append({
                    "tid": int(ids[k]) if ids else None,
                    "xyxy": [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)],
                    "kaki": [round((x1 + x2) / 2 / w, 4), round(y2 / h, 4)],
                    "conf": round(float(cf[k]), 3), "cls": int(cl[k]),
                    "zona": _zona_of(foot_px, zones) if zones else None,
                })
        count = sum(1 for d in dets if d["zona"] and d["zona"] not in LUAR)
        out.append({"t": t, "wh": [w, h], "count": count, "dets": dets})

    if cache:
        os.makedirs(TRACE_DIR, exist_ok=True)
        json.dump({"clip": clip, "model": model, "conf": conf, "frames": out}, open(cp, "w"))
    print(f"[trace] {os.path.basename(clip)} -> {len(out)} frame, {sum(len(f['dets']) for f in out)} deteksi"
          f"{' (cache)' if cache and os.path.exists(cp) else ''}", flush=True)
    return out


# ══ RULE: replay_* -> rules.py (di-import di atas) ═════════════════════════════════


# ══ VISUAL: numpang penonton umum (bukan app) ═══════════════════════════════════
def _draw(fr, f, lines, w, h):
    for d in f["dets"]:
        x1, y1, x2, y2 = int(d["xyxy"][0] * w), int(d["xyxy"][1] * h), int(d["xyxy"][2] * w), int(d["xyxy"][3] * h)
        cv2.rectangle(fr, (x1, y1), (x2, y2), C_BOX, 2)
        cv2.putText(fr, f'{d["conf"]:.2f}', (x1, max(11, y1 - 4)), 0, 0.42, C_BOX, 1, cv2.LINE_AA)
        cv2.circle(fr, (int(d["kaki"][0] * w), int(d["kaki"][1] * h)), 4, C_FOOT, -1)
    for g in lines:
        a, b = g["garis"]
        cv2.line(fr, (int(a[0] * w), int(a[1] * h)), (int(b[0] * w), int(b[1] * h)), C_LINE, 2)


def overlay_video(clip, tr, out="/tmp/overlay.mp4", lines=None, events=None, step=1):
    """Tulis mp4 beranotasi (kotak+kaki+garis+timestamp, kedip merah saat event) ->
    tonton di VLC/mpv/browser. Baca .ts berurutan (aman)."""
    tri = _garis_lines(lines) if lines else []
    evt = {}
    for e in (events or []):
        evt.setdefault(round(e["at"], 1), e)
    ti = {f["t"]: f for f in tr}
    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    w, h = int(cap.get(3)), int(cap.get(4))
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), max(1.0, fps / step), (w, h))
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i % step == 0:
            t = round(i / fps, 3)
            f = ti.get(t)
            if f:
                _draw(fr, f, tri, w, h)
            cv2.putText(fr, f"{t:6.1f}s", (8, 20), 0, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            e = evt.get(round(t, 1))
            if e:
                cv2.rectangle(fr, (0, 0), (w - 1, h - 1), C_EVENT, 8)
                cv2.putText(fr, str(e.get("arah") or e.get("kind")), (8, h - 12), 0, 0.7, C_EVENT, 2, cv2.LINE_AA)
            vw.write(fr)
        i += 1
    cap.release()
    vw.release()
    print(f"[overlay] {out}  ({len(evt)} event ditandai)", flush=True)
    return out


def contact_sheet(clip, out="/tmp/sheet.jpg", start=0.0, end=None, n=10, cols=5, tr=None, lines=None):
    """Kisi n frame (start..end detik) jadi SATU jpg -> buka inline di editor. Baca
    berurutan (pilih frame di indeks target) — tak seek .ts (aman)."""
    tri = _garis_lines(lines) if lines else []
    ti = {f["t"]: f for f in (tr or [])}
    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    end = end if end is not None else (total / fps if total else start + 4)
    want = sorted({int(round((start + (end - start) * k / max(1, n - 1)) * fps)) for k in range(n)})
    picks, i, wi = [], 0, 0
    while wi < len(want):
        ok, fr = cap.read()
        if not ok:
            break
        if i == want[wi]:
            t = round(i / fps, 3)
            f = ti.get(t)
            if f:
                _draw(fr, f, tri, fr.shape[1], fr.shape[0])
            cv2.putText(fr, f"{t:5.1f}s", (6, 18), 0, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            picks.append(fr)
            wi += 1
        i += 1
    cap.release()
    if not picks:
        print("[sheet] tak ada frame"); return None
    h, w = picks[0].shape[:2]
    while len(picks) % cols:
        picks.append(np.zeros((h, w, 3), np.uint8))
    rows = [cv2.hconcat(picks[r:r + cols]) for r in range(0, len(picks), cols)]
    cv2.imwrite(out, cv2.vconcat(rows))
    print(f"[sheet] {out}  ({len(want)} frame, {cols} kolom)", flush=True)
    return out


def plot_signals(tr, events=None, out=None, signal="count"):
    """Grafik sinyal-vs-waktu + garis merah di tiap event -> debug RULE ('kenapa tak
    jalan'). signal: 'count' (butuh zone_file saat trace) | 'kaki_x' | 'kaki_y' | 'n_det'.
    out=path -> simpan png (Agg); None -> jendela interaktif."""
    import matplotlib
    if out:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ts = [f["t"] for f in tr]

    def first_kaki(f, ax):
        return f["dets"][0]["kaki"][ax] if f["dets"] else None
    ys = {"count": [f["count"] for f in tr],
          "n_det": [len(f["dets"]) for f in tr],
          "kaki_x": [first_kaki(f, 0) for f in tr],
          "kaki_y": [first_kaki(f, 1) for f in tr]}[signal]

    fig, ax = plt.subplots(figsize=(11, 3.2))
    ax.plot(ts, ys, drawstyle="steps-post" if signal in ("count", "n_det") else "default",
            marker="." if signal.startswith("kaki") else None, lw=1.5)
    for e in (events or []):
        ax.axvline(e["at"], color="crimson", ls="--", lw=1)
        ax.text(e["at"], ax.get_ylim()[1], str(e.get("arah") or e.get("kind")),
                rotation=90, va="top", fontsize=8, color="crimson")
    ax.set_xlabel("detik"); ax.set_ylabel(signal); ax.grid(alpha=.25)
    fig.tight_layout()
    if out:
        fig.savefig(out, dpi=120); print(f"[plot] {out}", flush=True)
    else:
        plt.show()
    return out


# ══ CLI ═════════════════════════════════════════════════════════════════════════
def _parse_lines(specs):
    out = []
    for i, s in enumerate(specs or []):
        v = [float(x) for x in s.split(",")]
        out.append({"nama": f"garis{i + 1}", "garis": [[v[0], v[1]], [v[2], v[3]]]})
    return out


def main():
    ap = argparse.ArgumentParser(description="Coba rule/model dari satu klip (trace+overlay+sheet).")
    ap.add_argument("clip")
    ap.add_argument("--model", default="yolo11s.pt")
    ap.add_argument("--conf", type=float, default=0.20)
    ap.add_argument("--tracker", default=None, help="mis. botsort_reid.yaml (default: predict)")
    ap.add_argument("--zone-file", default=None, help="untuk count/plot scene")
    ap.add_argument("--lines", nargs="*", help='garis tripwire "x1,y1,x2,y2" (fraksi), boleh banyak')
    ap.add_argument("--out-dir", default="/tmp")
    args = ap.parse_args()

    tr = trace(args.clip, model=args.model, conf=args.conf, tracker=args.tracker, zone_file=args.zone_file)
    lines = _parse_lines(args.lines)
    if lines:
        cross = replay_tripwire(tr, lines)
        print(f"[tripwire] {len(cross)} penyeberangan:", [(c["garis"], c["arah"], c["at"]) for c in cross])
    else:
        cross = []
    base = os.path.splitext(os.path.basename(args.clip))[0]
    overlay_video(args.clip, tr, os.path.join(args.out_dir, base + "_ov.mp4"), lines=lines, events=cross)
    contact_sheet(args.clip, os.path.join(args.out_dir, base + "_sheet.jpg"), tr=tr, lines=lines)


if __name__ == "__main__":
    main()
