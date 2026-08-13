#!/usr/bin/env python3
"""Skala koordinat zona dari satu resolusi ke resolusi lain.

Zona (poligon di zones.json) digambar pada SATU resolusi tertentu — mis.
main-stream 2304x1296. Kalau pipeline pindah ke stream resolusi lain — mis.
sub-stream 640x360 — koordinat poligon HARUS diskalakan, kalau tidak zona
jatuh di luar frame dan tak pernah terpicu (kaki orang tak masuk poligon).

Koordinat zona bersifat RESOLUSI-DEPENDEN. Ini akar bug 2026-07-30:
zona 2304x1296 dipakai apa adanya di frame 640x360 -> nol deteksi.

Contoh:
  uv run pipeline/scale_zones.py zones.json --from 2304x1296 --to 640x360 -o zones-102.json
"""
import json
import argparse


def parse_res(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def scale_zones(data, sx, sy):
    out = {k: v for k, v in data.items() if k != "zones"}
    out["zones"] = {
        name: [[round(x * sx), round(y * sy)] for x, y in pts]
        for name, pts in data["zones"].items()
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="zones.json sumber")
    ap.add_argument("--from", dest="src", required=True, help="resolusi asal WxH, mis. 2304x1296")
    ap.add_argument("--to", dest="dst", required=True, help="resolusi tujuan WxH, mis. 640x360")
    ap.add_argument("-o", "--out", required=True, help="file zones hasil")
    args = ap.parse_args()

    sw, sh = parse_res(args.src)
    dw, dh = parse_res(args.dst)
    sx, sy = dw / sw, dh / sh

    data = json.loads(open(args.input).read())
    out = scale_zones(data, sx, sy)
    json.dump(out, open(args.out, "w"), indent=2)

    allx = [x for pts in out["zones"].values() for x, _ in pts]
    ally = [y for pts in out["zones"].values() for _, y in pts]
    print(f"{args.input} ({sw}x{sh}) -> {args.out} ({dw}x{dh})  skala x={sx:.4f} y={sy:.4f}")
    print(f"  rentang hasil: x max={max(allx)} (<= {dw}), y max={max(ally)} (<= {dh})")


if __name__ == "__main__":
    main()
