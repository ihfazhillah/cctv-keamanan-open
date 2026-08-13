#!/usr/bin/env bash
# Peluncur CCTV Viewer: masuk ke folder repo lalu jalankan viewer (jendela-app).
# Dipanggil oleh cctv-viewer.desktop; juga bisa dijalankan langsung.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DIR"
exec uv run pipeline/viewer.py "$@"
