"""Pemilihan encoder video — satu sumber untuk klip, episode, NVR-grab.

Tujuan: pipeline jalan DI MANA SAJA (termasuk laptop tanpa GPU NVIDIA) supaya bisa
belajar/praktik pada video yang sudah di-download. Deteksi encoder terbaik yang tersedia
sekali (cache), lalu pakai itu:
    h264_nvenc (GPU) -> libx264 -> libopenh264 -> mpeg4 (selalu ada).
"""
import subprocess
import functools


@functools.lru_cache(maxsize=1)
def h264_encoder():
    """Nama encoder H.264 terbaik yang tersedia di ffmpeg host ini (dideteksi sekali)."""
    try:
        keluaran = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
    except FileNotFoundError:
        return "libx264"                      # ffmpeg tak ada -> biar error jelas saat encode
    for enc in ("h264_nvenc", "libx264", "libopenh264"):
        if enc in keluaran:
            return enc
    return "mpeg4"                            # cadangan universal (selalu tersedia)


def reencode_h264(raw_name, final_name, duration=None):
    """mp4v mentah -> H.264 + faststart. duration=None -> encode SEMUA frame (episode
    streaming sudah pas); angka -> pangkas ke sekian detik (klip window)."""
    cmd = ["ffmpeg", "-i", raw_name]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-c:v", h264_encoder(), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-y", final_name]
    subprocess.run(cmd, check=True)
