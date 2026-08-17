#!/usr/bin/env python3
"""Retensi out/live: arsip klip+snap event bergulir SIZE-BASED.

Sejak SKIP_PIPELINE_CLIP=1 inti tak menulis out/live lagi, tapi ~16G legacy tetap ada
(dan bila flag dimatikan, pertumbuhan lanjut tanpa batas). Skrip ini menjaga
out/live <= OUT_LIVE_MAX_GB: hapus berkas TERTUA (mtime) sampai di bawah cap, TAPI
selalu sisakan >=KEEP berkas terbaru. Dipanggil sekali + berkala (systemd timer).

Zero-deps (stdlib). Logika pilih-hapus murni & teruji -> lihat test_retensi_live.py.
Pola meniru housekeeping segrec (SIZE-based, hapus tertua, sisakan >=N terbaru).
"""
import argparse
import os


def pilih_hapus(items, cap_bytes, keep):
    """items: iterable (path, size, mtime). -> list path utk dihapus.

    Buang yang TERTUA dulu (mtime naik) sampai total <= cap_bytes, tapi JANGAN
    pernah menyentuh `keep` berkas terbaru (jaring pengaman). Deterministik.
    """
    urut = sorted(items, key=lambda it: (it[2], it[0]))     # mtime asc; path utk tie-break stabil
    total = sum(sz for _, sz, _ in urut)
    n = len(urut)
    hapus = []
    i = 0
    while total > cap_bytes and i < n - keep:
        path, sz, _ = urut[i]
        hapus.append(path)
        total -= sz
        i += 1
    return hapus


def scan(dir_path):
    """out/live datar -> list (path, size, mtime) utk semua berkas biasa."""
    items = []
    with os.scandir(dir_path) as it:
        for e in it:
            if not e.is_file():
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            items.append((e.path, st.st_size, st.st_mtime))
    return items


def main():
    ap = argparse.ArgumentParser(description="Retensi size-based out/live")
    ap.add_argument("--dir", default="out/live")
    ap.add_argument("--cap-gb", type=float,
                    default=float(os.environ.get("OUT_LIVE_MAX_GB", "5")))
    ap.add_argument("--keep", type=int, default=int(os.environ.get("OUT_LIVE_KEEP", "200")),
                    help="jaring pengaman: minimal berkas terbaru yang tak disentuh")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"[RETENSI] dir tak ada: {args.dir} -> lewati")
        return

    items = scan(args.dir)
    total = sum(sz for _, sz, _ in items)
    cap = args.cap_gb * 1e9
    hapus = pilih_hapus(items, cap, args.keep)
    bytes_hapus = 0
    for p in hapus:
        try:
            bytes_hapus += os.path.getsize(p)
        except OSError:
            pass

    tag = "DRY-RUN" if args.dry_run else "HAPUS"
    print(f"[RETENSI] {args.dir}: {len(items)} berkas / {total/1e9:.2f} GB | "
          f"cap={args.cap_gb} GB keep={args.keep} -> {tag} {len(hapus)} berkas "
          f"/ {bytes_hapus/1e9:.2f} GB | sisa ~{(total-bytes_hapus)/1e9:.2f} GB")
    if args.dry_run:
        return
    n = 0
    for p in hapus:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    print(f"[RETENSI] selesai: terhapus {n}/{len(hapus)} berkas")


if __name__ == "__main__":
    main()
