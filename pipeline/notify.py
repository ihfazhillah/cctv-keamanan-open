import os
import sqlite3
import time
import subprocess
import argparse
import datetime
import requests

from pathlib import Path

from store import detect_passages


TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
IS_ARMED = os.environ.get("IS_ARMED", "0") # for now use 1/0 and check as string

PRE = 5 # in detik
POST = 10 # in detik

DB = Path("out/events.db")


def send_video(clip_path, caption):
    url = f"https://api.telegram.org/bot{TOKEN}/sendVideo"
    with open(clip_path, "rb") as f:
        r = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"video": f})
        r.raise_for_status()


def send_text(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    r.raise_for_status()


def cut_clip(video, start_s, end_s, pre, post, out):
    clip_start = max(0, start_s - pre)
    clip_end = end_s + post
    duration = clip_end - clip_start
    command = ["ffmpeg", "-ss", str(clip_start), "-i", video, "-t", str(duration), "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "faststart", "-y", out]
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", help="video asal") # ini nanti kita buang kalau pakai rtsp... supaya tidak hardcode saja
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    cur = con.cursor()

    """ DISABLED IT
    # it's may be another, jadi nanti
    # kalau bisa di query, kita build untuk tidak
    # mengetahui jumlah yang dicari
    zones = ("teras", "jalan-masuk", "tangga")

    query = "SELECT start_s, end_s, zone, start_wall FROM episodes WHERE zone IN " 

    question_marks = ["?" for _ in range(len(zones))]
    variables = "(" + ", ".join(question_marks) + ")"

    query += variables


    for row in cur.execute(query, zones):
        dwell = (row[1] - row[0])
        satuan = "detik"
        if dwell > 60:
            dwell = dwell / 60
            satuan = "menit"

        if IS_ARMED == "1":
            message = f"⚠️ ADA ORANG ⚠️\n📍 {row[2]}\n⏳️ {dwell:.2f} {satuan} ({row[0]:.1f}s -> {row[1]:.1f}s)\n🗓 {row[3]}"
            out_name = f"out/clip_{row[3]}_{row[0]:.1f}_{row[1]:.1f}.mp4"
            cut_clip(args.video, row[0], row[1], PRE, POST, out_name)

            send_video(out_name, message)
            time.sleep(1) # untuk sekarang... supaya tidak dianggap spam
    """

    abaikan = {"jalan-utama"}
    property_eps = []
    for start_s, end_s, zone, start_wall in cur.execute(
        "SELECT start_s, end_s, zone, start_wall FROM episodes"
    ):
        if zone in abaikan:
            continue

        dt_start = datetime.datetime.fromisoformat(start_wall)
        dt_end = dt_start + datetime.timedelta(seconds=end_s - start_s) # durasi == end - start
        property_eps.append((dt_start, dt_end, zone, start_s, end_s))

    ambang = datetime.timedelta(seconds=3)

    for pas in detect_passages(property_eps, ambang):
        if IS_ARMED == "1":
            caption = f"- {pas['kind']}\n🗓{pas['wall']:%Y-%m-%d %H:%M:%S}"
            out_name = f"out/clip_{pas['kind']}_{pas['video_s']:.1f}.mp4"
            cut_clip(args.video, pas['video_s'], pas['video_s'], PRE, POST, out_name)
            send_video(out_name, caption)
            time.sleep(1)

    con.close()


if __name__ == "__main__":
    main()
