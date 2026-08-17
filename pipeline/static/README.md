# pipeline/static

Aset statis yang disajikan viewer.py lewat rute `/static/`.

## hls.light.min.js
- **hls.js** v1.5.17 (build "light": tanpa alt-audio/EME/subtitle — cukup utuk VOD 1 stream).
- Sumber: https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.light.min.js
- Lisensi: Apache-2.0 (https://github.com/video-dev/hls.js).
- Dipakai untuk memutar arsip HLS di mode "Arsip" (Chrome/Firefox tak putar HLS/.ts
  native). Di-vendor agar viewer tetap jalan offline & tanpa CDN eksternal.
