#!/usr/bin/env bash
# Pasang ikon "CCTV Viewer" ke menu aplikasi (satu kali). Jalankan:  desktop/install.sh
# Setelah ini: cari "CCTV Viewer" di menu / launcher, klik -> interface terbuka.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
chmod +x "$DIR/desktop/launch.sh"

cat > "$APPS/cctv-viewer.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CCTV Viewer
Comment=Telusuri & putar rekaman event CCTV rumah
Exec=$DIR/desktop/launch.sh
Icon=$DIR/desktop/cctv-viewer.svg
Terminal=false
Categories=AudioVideo;Utility;
StartupNotify=true
EOF

update-desktop-database "$APPS" 2>/dev/null || true
echo "Terpasang: 'CCTV Viewer' di menu aplikasi."
echo "Klik ikonnya -> server jalan + jendela app terbuka. Tutup jendela -> berhenti."
