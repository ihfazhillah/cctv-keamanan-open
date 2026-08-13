# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "ultralytics>=8.3",
#   "supervision>=0.25",
# ]
# ///
"""Lesson 0003 — tracking: dari kotak per-frame menjadi identitas.

SKELETON: bagian bertanda TODO 1/2/3 ANDA yang menulis. Spesifikasi, petunjuk
desain (dari data eksperimen Anda sendiri), dan kriteria lulus ada di
lessons/0003-tracking-dari-kotak-menjadi-identitas.html.

    uv run pipeline/track.py samples/siang_3m.mp4

Kriteria lulus di golden clip: 2 anak = 2 track person yang stabil
(pintu → pagar → kolam ikan → kembali), tanpa banjir ID baru.
"""
import argparse
import time
from pathlib import Path

import numpy as np
import supervision as sv
from ultralytics import YOLO


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
    args = p.parse_args()

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"

    model = YOLO(args.model)
    video_info = sv.VideoInfo.from_video_path(args.video)
    out_path = Path(args.out or f"out/{Path(args.video).stem}_tracked.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"model  : {args.model}  |  device: {device}  |  det-conf >= {args.det_conf}"
          f"  |  imgsz {args.imgsz}")
    print(f"video  : {video_info.width}x{video_info.height} @ {video_info.fps}fps, "
          f"{video_info.total_frames} frames")

    # ------------------------------------------------------------------
    # TODO 1 — buat tracker-nya.
    # Baca: https://supervision.roboflow.com/latest/trackers/
    # Setel SADAR (jangan telan default) lima parameter ini:
    #   track_activation_threshold  confidence minimum untuk MEMULAI track
    #   lost_track_buffer           berapa frame track ditahan saat deteksi hilang
    #   minimum_matching_threshold  ambang kecocokan (IoU) kotak antar frame
    #   frame_rate                  fps video Anda — HATI-HATI, video Anda bukan 30
    #   minimum_consecutive_frames  berapa frame beruntun sebelum track dianggap sah
    # Petunjuk memilih angkanya dari data Anda sendiri: lesson bagian 3.
    # ------------------------------------------------------------------
    tracker = sv.ByteTrack(
        track_activation_threshold=0.3,
        lost_track_buffer=5 * video_info.fps, # 5 detik
        frame_rate=video_info.fps,
        minimum_matching_threshold=0.8, # default, 0.9 menyatukan id namun terjadi banyak swap id
        minimum_consecutive_frames=5 # filter-out ketika hanya terdetksi <5 frame berurutan
    )

    wanted = args.classes.split(",")
    box_annotator = sv.BoxAnnotator(thickness=2)
    label_annotator = sv.LabelAnnotator(text_scale=0.5)
    trace_annotator = sv.TraceAnnotator(trace_length=60)  # jejak lintasan ±3 detik

    track_log: dict[int, dict] = {}  # tracker_id -> riwayat; DIISI oleh TODO 3
    boxes_in = 0    # kotak masuk tracker (sesudah whitelist)
    boxes_out = 0   # kotak keluar tracker (punya tracker_id)
    frames = 0
    t_start = time.perf_counter()

    with sv.VideoSink(str(out_path), video_info) as sink:
        for frame in sv.get_video_frames_generator(args.video):
            result = model(frame, conf=args.det_conf, imgsz=args.imgsz,
                           device=device, verbose=False)[0]
            detections = sv.Detections.from_ultralytics(result)
            detections = detections[np.isin(detections.data["class_name"], wanted)]
            boxes_in += len(detections)

            # ----------------------------------------------------------
            # TODO 2 — lewatkan detections ke tracker.
            # Satu pemanggilan method pada `tracker`. Hasilnya: detections
            # yang sudah tersaring dua-tingkat DAN punya detections.tracker_id.
            # Untuk laporan Anda: kenapa jumlah kotak SETELAH baris ini jauh
            # lebih sedikit daripada sebelumnya, padahal det-conf cuma 0.05?
            # Jawaban: dikarenakan kita sudah drop-out semua yang tidak memiliki frame frame yang memiliki id yang sama
            # ----------------------------------------------------------
            detections = tracker.update_with_detections(detections)

            boxes_out += len(detections)

            # ----------------------------------------------------------
            # TODO 3 — catat riwayat tiap track ke track_log.
            # Untuk setiap deteksi ber-tracker_id di frame ini, pastikan:
            #   track_log[tid] = {"class_name": <str>,
            #                     "first_frame": <int, diisi sekali>,
            #                     "last_frame":  <int, terus maju>,
            #                     "n_frames":    <int, +1 tiap frame terlihat>}
            # Blok ringkasan di bawah membaca struktur ini — jangan ubah kuncinya.
            # ----------------------------------------------------------
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
            sink.write_frame(annotated)

            frames += 1
            if frames % 100 == 0:
                print(f"\r  {frames}/{video_info.total_frames} frames...", end="",
                      flush=True)
            if args.limit and frames >= args.limit:
                break

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
