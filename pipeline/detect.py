# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ultralytics>=8.3",
#   "supervision>=0.25",
# ]
# ///
"""Lesson 0002 — deteksi pertama pada video CCTV sendiri.

Jalankan dari root repo di PC praktik:

    uv run pipeline/detect.py samples/siang.mp4
    uv run pipeline/detect.py samples/siang.mp4 --conf 0.1
    uv run pipeline/detect.py samples/siang.mp4 --model yolo11m.pt --classes person,cat

Output: video beranotasi di out/<nama>_annotated.mp4 + ringkasan statistik di terminal.
Script ini SENGAJA hanya kotak #1 (ingest) dan #2 (detect) dari peta lesson 0001 —
tanpa tracker, tanpa zona. Perhatikan apa yang hilang karenanya.
"""
import argparse
import time
from collections import Counter
from pathlib import Path

import numpy as np
import supervision as sv
from ultralytics import YOLO


def main() -> None:
    p = argparse.ArgumentParser(description="Deteksi objek pada satu file video.")
    p.add_argument("video", help="path file video, mis. samples/siang.mp4")
    p.add_argument("--model", default="yolo11s.pt",
                   help="bobot model (n/s/m/l/x); auto-download saat pertama kali")
    p.add_argument("--conf", type=float, default=0.25,
                   help="ambang confidence 0..1 (default 0.25)")
    p.add_argument("--classes", default=None,
                   help="filter nama kelas COCO, dipisah koma, mis. person,cat")
    p.add_argument("--limit", type=int, default=None,
                   help="berhenti setelah N frame (untuk uji cepat)")
    p.add_argument("--out", default=None, help="path video output")
    p.add_argument("--imgsz", type=int, default=640)
    args = p.parse_args()

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"

    model = YOLO(args.model)
    video_info = sv.VideoInfo.from_video_path(args.video)
    out_path = Path(args.out or f"out/{Path(args.video).stem}_annotated.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"model  : {args.model}  |  device: {device}  |  conf >= {args.conf}")
    print(f"video  : {video_info.width}x{video_info.height} @ {video_info.fps}fps, "
          f"{video_info.total_frames} frames")

    wanted = set(args.classes.split(",")) if args.classes else None
    box_annotator = sv.BoxAnnotator(thickness=2)
    label_annotator = sv.LabelAnnotator(text_scale=0.5)

    box_counts: Counter = Counter()      # total kotak per kelas (BUKAN jumlah objek!)
    frames_with: Counter = Counter()     # frame yang memuat >=1 kotak kelas tsb
    frames = 0
    infer_seconds = 0.0
    t_start = time.perf_counter()

    imgsz = args.imgsz

    with sv.VideoSink(str(out_path), video_info) as sink:
        for frame in sv.get_video_frames_generator(args.video):
            t0 = time.perf_counter()
            result = model(frame, conf=args.conf, device=device, imgsz=imgsz, verbose=False)[0]
            infer_seconds += time.perf_counter() - t0

            detections = sv.Detections.from_ultralytics(result)
            if wanted is not None:
                detections = detections[
                    np.isin(detections.data["class_name"], list(wanted))
                ]

            names = detections.data["class_name"]
            box_counts.update(names)
            frames_with.update(set(names))

            labels = [
                f"{name} {conf:.2f}"
                for name, conf in zip(names, detections.confidence)
            ]
            annotated = box_annotator.annotate(scene=frame.copy(), detections=detections)
            annotated = label_annotator.annotate(scene=annotated, detections=detections,
                                                 labels=labels)
            sink.write_frame(annotated)

            frames += 1
            if frames % 100 == 0:
                print(f"\r  {frames}/{video_info.total_frames} frames...", end="",
                      flush=True)
            if args.limit and frames >= args.limit:
                break

    wall = time.perf_counter() - t_start
    print(f"\n{'=' * 52}")
    print(f"frames dianalisis : {frames}  ({wall:.1f}s wall, "
          f"{frames / wall:.1f} fps end-to-end)")
    print(f"kecepatan model   : {frames / infer_seconds:.1f} fps (inferensi saja)")
    print("kotak per kelas   : (total kotak, bukan jumlah objek — pikirkan kenapa)")
    for name, n in box_counts.most_common():
        print(f"  {name:<12} {n:>7} kotak  |  muncul di {frames_with[name]} frame")
    if not box_counts:
        print("  (tidak ada deteksi — coba turunkan --conf)")
    print(f"video beranotasi  : {out_path}")


if __name__ == "__main__":
    main()
