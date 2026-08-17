#!/usr/bin/env python3
"""Supervisor CCTV — SATU service menjalankan SEMUA kamera dari cameras.json.

Kelola sub-proses per-alur (isolasi penuh + pakai ulang kode apa adanya):
  - peran 'taman-penuh'  -> satu `run_live.py` (pipeline penuh) per kamera.
  - peran 'garasi-ringan' -> satu `run_garasi.py` (multi-kamera; semua kamera ringan,
    model yolo11s dibagi di dalamnya).

Tambah/hapus/enable/ubah kamera = edit cameras.json (dari viewer/Telegram) ->
supervisor start/stop/restart sub-proses LIVE (via mtime), TANPA systemctl.
Sub-proses mati (stream putus / crash) -> supervisor restart. SIGTERM -> hentikan
semua anak dengan rapi. Env (.env) diwarisi anak dari supervisor.

    uv run --env-file .env pipeline/run_supervisor.py --cameras-file cameras.json

Logika murni (build cmd, desired) teruji -> test_run_supervisor.py.
"""
import os
import sys
import json
import time
import signal
import argparse
import subprocess
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
PIPE = os.path.dirname(os.path.abspath(__file__))                    # pipeline/
PY = sys.executable                                                  # venv python (uv)


def read_cameras(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"kamera": []}


def stream_url(nama):
    return f"rtsp://localhost:8554/{nama}"


def build_taman_cmd(cam):
    """Argumen run_live.py untuk satu kamera taman-penuh (mirror cctv.service lama)."""
    cmd = [PY, os.path.join(PIPE, "run_live.py"), stream_url(cam["stream"]),
           "--zone-file", str(cam.get("zone_file", "zones.json")),
           "--model", str(cam.get("model", "yolo26l.pt")),
           "--conf", str(cam.get("conf", 0.15))]
    if cam.get("loiter_s") is not None:
        cmd += ["--loiter_s", str(cam["loiter_s"])]
    return cmd


def build_garasi_cmd(cameras_file):
    return [PY, os.path.join(PIPE, "run_garasi.py"), "--cameras-file", cameras_file]


def build_segrec_cmd():
    return [PY, os.path.join(ROOT, "segrec", "run_segrec.py")]


def segrec_env(cam):
    """Env per-instance segrec: 1 kamera -> subfolder + health/tmp/cap sendiri
    (tak collide antar-instance). run_segrec sudah 100% env-driven -> nol ubah kode."""
    nama = cam["nama"]
    return {
        "SEGREC_URL": stream_url(cam["stream"]),
        "SEG_DIR": os.path.join("out/segments", nama),
        "SEG_MAX_GB": str(cam.get("rekam_gb", 30)),
        "SEGREC_HEALTH": f"out/segrec-health-{nama}.json",
        "SEGREC_CLIPTMP": f"out/segrec-selftest-{nama}.mp4",
    }


def desired_procs(cfg, cameras_file):
    """key -> (label, cmd, env). Per kamera enabled: taman-penuh -> run_live;
    garasi-ringan -> (satu) run_garasi utk semua ringan; rekam!=false -> run_segrec
    (subfolder out/segments/<nama>). env=None -> warisi (live/garasi)."""
    procs = {}
    ada_garasi = False
    for k in cfg.get("kamera", []):
        if not (k.get("enabled", True) and k.get("nama") and k.get("stream")):
            continue
        if k.get("peran") == "taman-penuh":
            procs[("live", k["nama"])] = (f"live:{k['nama']}", build_taman_cmd(k), None)
        elif k.get("peran") == "garasi-ringan":
            ada_garasi = True
        if k.get("rekam", True):
            procs[("segrec", k["nama"])] = (f"segrec:{k['nama']}", build_segrec_cmd(), segrec_env(k))
    if ada_garasi:
        procs[("garasi",)] = ("garasi", build_garasi_cmd(cameras_file), None)
    return procs


def main():
    ap = argparse.ArgumentParser(description="Supervisor CCTV multi-kamera (satu service)")
    ap.add_argument("--cameras-file", default="cameras.json")
    ap.add_argument("--interval", type=float, default=3.0, help="detik antar rekonsiliasi")
    args = ap.parse_args()
    cameras_file = args.cameras_file

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))

    running = {}          # key -> {"proc": Popen, "cmd": [...], "label": str}
    mtime = None
    cfg = read_cameras(cameras_file)
    print(f"[SUPER] mulai cameras-file={cameras_file} root={ROOT}", flush=True)

    def spawn(key, label, cmd, env):
        print(f"[SUPER] start {label}", flush=True)
        e = {**os.environ, **env} if env else None   # None -> warisi penuh (.env supervisor)
        p = subprocess.Popen(cmd, cwd=ROOT, env=e)
        running[key] = {"proc": p, "cmd": cmd, "env": env, "label": label}

    def kill(key):
        info = running.pop(key, None)
        if not info:
            return
        print(f"[SUPER] stop {info['label']}", flush=True)
        p = info["proc"]
        try:
            p.terminate()
            p.wait(timeout=15)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    try:
        while not stop["v"]:
            try:
                m = os.path.getmtime(cameras_file)
            except OSError:
                m = None
            if m != mtime:
                mtime = m
                cfg = read_cameras(cameras_file)
                print("[SUPER] cameras.json dimuat", flush=True)
            want = desired_procs(cfg, cameras_file)
            for key in list(running):                                  # stop yg tak diinginkan / cmd|env berubah
                if key not in want or (want[key][1], want[key][2]) != (running[key]["cmd"], running[key]["env"]):
                    kill(key)
            for key, (label, cmd, env) in want.items():                # start baru / restart mati
                info = running.get(key)
                if info is None:
                    spawn(key, label, cmd, env)
                elif info["proc"].poll() is not None:
                    print(f"[SUPER] {label} mati (rc={info['proc'].returncode}) -> restart", flush=True)
                    spawn(key, label, cmd, env)
            time.sleep(args.interval)
    finally:
        for key in list(running):
            kill(key)
        print("[SUPER] berhenti.", flush=True)


if __name__ == "__main__":
    main()
