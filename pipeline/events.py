# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ultralytics>=8.3",
#   "supervision>=0.25",
# ]
# ///
"""Lesson 0004-5 — baris log pertama system.
"""

import argparse
import time
from pathlib import Path
import json
from enum import Enum


import numpy as np
import supervision as sv
from ultralytics import YOLO
import cv2


class Status(Enum):
    KOSONG = 0
    TERISI = 1


class FileLogger:
    event_log = Path("out/events.jsonl")

    def clear(self):
        self.event_log.write_text("")

    def log(self, ev):
        with self.event_log.open("a") as f:
            f.write(json.dumps(ev) + "\n")


class ConsoleLogger:
    def log(self, ev):
        print(f"EPISODE {ev['zone']}: {ev['start_s']}s -> {ev['end_s']}s (dwell {ev['dwell_s']}s)")


class PathTracker:
    """Track-sentris: ikuti tiap track_id, catat saat dia PINDAH zona."""

    def __init__(self, fps, loggers=None):
        self.last_zone = {} # track_id -> nama zona terakhirnya (None = diluar semua zone)
        self.fps = fps
        self.loggers = loggers 

    def update(self, frame_idx, tid_to_zone):
        # tid_to_zone: {track_id: nama_zone | None} untuk FRAME INI
        for tid, zona in tid_to_zone.items():
            prev = self.last_zone.get(tid) # None kalau track ini baru pertama terlihat

            if zona != prev:
                self.log_transition(frame_idx, tid, prev, zona)
                self.last_zone[tid] = zona
            else:
                # no-op, no transition
                pass

    def log_transition(self, frame_idx, tid, prev, zona):
        ev = {
            "type": "zone_transition",
            "track_id": int(tid),
            "from": prev,
            "to": zona,
            "t_s": round(frame_idx / self.fps, 2),
        }
        print(f" #{tid:<4} {str(prev):>12} -> {str(zona):<12} at {ev['t_s']}s")
        if self.loggers:
            for logger in self.loggers:
                logger.log(ev)




class Episode:
    """Satu episode sebuah zone, dari kosong -> isi, menjadi kosong kembali"""

    def __init__(self, zone_name, enter_inertia, exit_hysteresis, fps, loggers=None):
        self.status = Status.KOSONG
        self.hadir_beruntun = 0
        self.kosong_beruntun = 0
        self.episode = None
        self.episodes = []
        self.current_count = 0

        self.enter_inertia = enter_inertia
        self.exit_hysteresis = exit_hysteresis
        self.fps = fps
        self.loggers = loggers
        self.zone_name = zone_name

    def track(self, frame_idx, count, track_ids):

        if self.status == Status.KOSONG:
            if count > 0:
                self.hadir_beruntun += 1
            else:
                self.hadir_beruntun = 0
                self.kosong_beruntun = 0

            if self.hadir_beruntun >= self.enter_inertia:
                self.buka_episode(frame_idx, count, track_ids)

        elif self.status == Status.TERISI:
            if count > 0:
                self.update_episode(frame_idx, count, track_ids)
            elif count == 0:
                self.kosong_beruntun += 1
                hysteresis_frames = self.exit_hysteresis * self.fps
                if self.kosong_beruntun >= hysteresis_frames:
                    self.tutup_episode()
        

    def buka_episode(self, frame_idx, count, track_ids):
        self.status = Status.TERISI
        self.hadir_beruntun = 0
        self.current_count = count
        self.episode = {
            "start_frame": frame_idx - self.enter_inertia + 1,
            "last_seen_frame": frame_idx,
            "track_ids": set(track_ids),
            "peak": count,
            "open_at_eof": False
        }


    def update_episode(self, frame_idx, count, track_ids):
        self.kosong_beruntun = 0
        self.current_count = count
        self.episode.update({
            "last_seen_frame": frame_idx,
            "track_ids": self.episode["track_ids"] | track_ids,
            "peak": max(self.episode["peak"], count)
        })

    def tutup_episode(self, paksa=False):
        self.current_count = 0
        if self.episode is None:
            return

        if paksa:
            self.episode["open_at_eof"] = True

        self.episodes.append(self.episode)
        self.log()

        self.status = Status.KOSONG
        self.episode = None

    def log(self):

        start_s = self.episode["start_frame"] / self.fps
        end_s = self.episode["last_seen_frame"] / self.fps
        dwell_s = end_s - start_s


        ev = {
            "zone": self.zone_name, "start_s": start_s, "end_s": end_s, "dwell_s": dwell_s,
            "type": "episode_tracker"
        }

        if self.episode:
            ev.update({
                "start_frame": self.episode["start_frame"],
                "end_frame": self.episode["last_seen_frame"],
                "track_ids": list(self.episode["track_ids"]),
                "peak_count": self.episode["peak"],
                "open_at_eof": self.episode["open_at_eof"]
            })

        if self.loggers:
            for logger in self.loggers:
                logger.log(ev)



def main() -> None:
    p = argparse.ArgumentParser(description="Deteksi + tracking pada satu file video.")
    p.add_argument("video", help="path file video, mis. samples/siang_3m.mp4")
    p.add_argument("--model", default="yolo11s.pt", help="bobot model deteksi")
    p.add_argument("--det-conf", type=float, default=0.05,
                   help="ambang deteksi SENGAJA rendah — tracker yang memilah "
                        "(temuan lesson 0002: sinyal kolam hidup di 5-15%%)")
    p.add_argument("--classes", default="person",
                   help="whitelist kelas, dipisah koma (default: person)")
    p.add_argument("--imgsz", type=int, default=640,
                   help="resolusi sisi inferensi (default 640; 1280 menolong "
                        "objek kecil/teroklusi, lebih lambat)")
    p.add_argument("--limit", type=int, default=None,
                   help="berhenti setelah N frame (uji cepat)")
    p.add_argument("--out", default=None, help="path video output")
    p.add_argument("--enter-inertia", default=5, type=int, help="Kapan object bisa dinyatakan masuk ke dalam zona setelah bertahan berapa frame? satuan=frame")
    p.add_argument("--exit-hysteresis", default=4.0, type=float, help="Kapan object bisa dinyatakan keluar dari zona setelah menghilang berapa detik? satuan=detik")
    args = p.parse_args()

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"

    model = YOLO(args.model)
    video_info = sv.VideoInfo.from_video_path(args.video)
    out_path = Path(args.out or f"out/{Path(args.video).stem}_events.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"model  : {args.model}  |  device: {device}  |  det-conf >= {args.det_conf}"
          f"  |  imgsz {args.imgsz}")
    print(f"video  : {video_info.width}x{video_info.height} @ {video_info.fps}fps, "
          f"{video_info.total_frames} frames")



    wanted = args.classes.split(",")
    box_annotator = sv.BoxAnnotator(thickness=2)
    label_annotator = sv.LabelAnnotator(text_scale=0.5)
    trace_annotator = sv.TraceAnnotator(trace_length=60)  # jejak lintasan ±3 detik

    track_log: dict[int, dict] = {}  # tracker_id -> riwayat; DIISI oleh TODO 3
    boxes_in = 0    # kotak masuk tracker (sesudah whitelist)
    boxes_out = 0   # kotak keluar tracker (punya tracker_id)
    frames = 0
    t_start = time.perf_counter()

    trackers = {} # name -> Episode
    zones = {} # name -> sv.PolygonZone
    polygons = {} # name -> array

    file_logger = FileLogger()
    # make sure empty for each run
    file_logger.clear()

    console_logger = ConsoleLogger()

    zone_data = json.loads(Path("zones.json").read_text())
    for zone_name, points in zone_data["zones"].items():
        polygons[zone_name] = np.array(points, dtype=np.int32)
        zones[zone_name] = sv.PolygonZone(polygon=polygons[zone_name], triggering_anchors=[sv.Position.BOTTOM_CENTER])
        trackers[zone_name] = Episode(
                zone_name,
                args.enter_inertia, args.exit_hysteresis, video_info.fps,
                loggers=[file_logger, console_logger]
        )
    print(f"{len(zones)} zona dimuat: {','.join(zones.keys())}")


    path_tracker = PathTracker(video_info.fps, [file_logger])
    

    with sv.VideoSink(str(out_path), video_info) as sink:
        for frame in sv.get_video_frames_generator(args.video):
            result = model.track(frame, persist=True, tracker="botsort_reid.yaml", 
                                 conf=args.det_conf, imgsz=args.imgsz,
                           device=device, verbose=False)[0]

            detections = sv.Detections.from_ultralytics(result)
            detections = detections[np.isin(detections.data["class_name"], wanted)]
            boxes_in += len(detections)

            # model.track terkadang menghasilkan tracker_id None. Yaitu ketika tidak terdapat deteksi sama sekali
            # berbeda dengan ByteTrack sebelumnya. Dia akan mengembalikan empty array ketika tidak ada deteksi.
            if detections.tracker_id is None:
                detections = sv.Detections.empty()
                detections.tracker_id = np.array([], dtype=int)

            tid_to_zone = {int(t): None for t in detections.tracker_id} # default to none, dia belum assign track_id ke zona manapun. Yang tracker.update_with_detections tahu: ada track_id di suatu frame.

            for nama in zones:
                di_zona = zones[nama].trigger(detections)
                jumlah = int(di_zona.sum())
                trackers[nama].track(frames, jumlah, set(detections.tracker_id[di_zona].tolist()))

                for tid in detections.tracker_id[di_zona]:
                    tid_to_zone[int(tid)] = nama

            path_tracker.update(frames, tid_to_zone)


            boxes_out += len(detections)

            for tid, name in zip(detections.tracker_id, detections.data.get("class_name", [])):
                if tid not in track_log:
                    track_log[tid] = {
                        "class_name": name,
                        "first_frame": frames,
                        "last_frame": frames,
                        "n_frames": 1 # initial frame harusnya terhitung. Sebelumnya 0, berarti terdeteksi pertama tidak terhitung
                    }
                else:
                    track_log[tid]["last_frame"] = frames
                    track_log[tid]["n_frames"] += 1

                    

            labels = [
                f"#{tid} {name} {conf:.2f}"
                for tid, name, conf in zip(detections.tracker_id,
                                           detections.data.get("class_name", []), # fix karena kalau tidak ada deteksi, data adalah empty dict
                                           detections.confidence)
            ]
            annotated = trace_annotator.annotate(frame.copy(), detections)
            annotated = box_annotator.annotate(annotated, detections)
            annotated = label_annotator.annotate(annotated, detections, labels)

            for zone_name, polygon in polygons.items():
                color = (0, 0, 255)
                if trackers[zone_name].status == Status.TERISI:
                    # biru agak lama, dikarenakan masih ada menunggu beberapa saat sebelum
                    # status benar benar berubah
                    color = (255, 0, 0)

                cv2.polylines(annotated, [polygon], isClosed=True, color=color, thickness=2)

                if trackers[zone_name].status == Status.TERISI:
                    cv2.putText(annotated, f"{trackers[zone_name].current_count} person", polygon[0], cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)


            sink.write_frame(annotated)

            frames += 1
            if frames % 100 == 0:
                print(f"\r  {frames}/{video_info.total_frames} frames...", end="",
                      flush=True)
            if args.limit and frames >= args.limit:
                break

    # paksa tutup episode, karena mungkin episode masih terbuka
    # tapi video keburu habis
    for tracker in trackers.values():
        tracker.tutup_episode(paksa=True)

    # ---- ringkasan: sudah jadi, membaca track_log ANDA ----
    wall = time.perf_counter() - t_start
    print(f"\n{'=' * 60}")
    print(f"frames dianalisis : {frames}  ({wall:.1f}s, {frames / wall:.1f} fps end-to-end)")
    print(f"kotak → track     : {boxes_in} kotak masuk  →  {boxes_out} kotak "
          f"ber-ID  →  {len(track_log)} track unik")
    if not track_log:
        print("  (track_log kosong — TODO 3 belum mengisi apa pun)")
    for tid, info in sorted(track_log.items()):
        lifespan = info["last_frame"] - info["first_frame"] + 1
        dur = lifespan / video_info.fps
        gap = lifespan - info["n_frames"]
        print(f"  #{tid:<4} {info['class_name']:<8} "
              f"frame {info['first_frame']:>5}-{info['last_frame']:<5} "
              f"hidup {dur:6.1f}s | terlihat {info['n_frames']:>5} frame "
              f"| dijembatani {gap:>4} frame")
    print(f"video beranotasi  : {out_path}")
    print("\nGolden clip: idealnya 2 track person panjang. Track pendek berserakan")
    print("= fragmentasi (buffer kurang?); ID loncat antar anak = ID switch (catat!).")


if __name__ == "__main__":
    main()
