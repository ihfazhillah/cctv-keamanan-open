# Tutorial: go2rtc sebagai Gerbang Input (Fase 1)

Untuk dipelajari & dijalankan sendiri. Tujuan: satu tempat mendaftarkan **semua
sumber kamera** (IP cam, USB, node Orange Pi, kamera mobil) → dinormalkan jadi
RTSP → dikonsumsi pipeline + segrec.

## Kenapa go2rtc (dan kenapa ini menyelesaikan masalah kita)
- **Satu koneksi upstream per kamera**, di-fan-out ke banyak konsumen. Selama ini
  pipeline & segrec masing-masing buka koneksi → sempat bikin stall. Dengan go2rtc,
  **go2rtc yang pegang 1 koneksi ke kamera**, pipeline & segrec tinggal baca dari
  go2rtc. Kontensi hilang by design.
- **Input apa saja** jadi seragam: tambah sumber = tambah satu entri config.
- Web UI + WebRTC preview (low-latency) gratis.

```
kamera/NVR/USB/HP ──►  go2rtc (1 koneksi upstream, hub)  ──►  pipeline (deteksi)
                                                          └─►  segrec (rekam)
                                                          └─►  viewer / browser (WebRTC)
```

## 1. Pasang (Fedora)
go2rtc = 1 binary Go, tanpa dependensi. Unduh rilis terbaru:

```bash
mkdir -p ~/go2rtc && cd ~/go2rtc
# cek versi terbaru di https://github.com/AlexxIT/go2rtc/releases
curl -L -o go2rtc https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
chmod +x go2rtc
./go2rtc -version
```

## 2. Config dasar (kredensial dari ENV — jangan hardcode!)
go2rtc mengembangkan `${VAR}` dari environment. Simpan password di env, bukan di
file config. Buat `~/go2rtc/go2rtc.yaml`:

```yaml
streams:
  # kamera taman = channel NVR 202 (substream). Password dari env NVR_PASS.
  taman: rtsp://${NVR_USER}:${NVR_PASS}@${NVR_HOST}:554/Streaming/Channels/202

api:
  listen: ":1984"      # web UI
rtsp:
  listen: ":8554"      # server RTSP keluaran
```

⚠️ **Jangan commit `go2rtc.yaml` bila memuat host/kredensial.** Tambahkan ke
`.gitignore`. (Password tetap via `${NVR_PASS}` dari `.env`/environment.)

Jalankan dengan env dari `.env` project:
```bash
cd ~/go2rtc
env $(grep -v '^#' ~/Dev/cctv-keamanan-open/.env | xargs) ./go2rtc -config go2rtc.yaml
```

## 3. Verifikasi
- Buka **http://localhost:1984** → daftar stream `taman`, klik untuk preview WebRTC.
- Keluaran RTSP go2rtc: **`rtsp://localhost:8554/taman`** — inilah yang dipakai
  konsumen. Uji:
  ```bash
  ffprobe -rtsp_transport tcp rtsp://localhost:8554/taman
  ```
  Harus tampak video h264 (+ audio bila sumbernya bawa audio).

## 4. Arahkan pipeline + segrec ke go2rtc (bukan langsung ke kamera)
Setelah hub jalan, ganti sumber jadi go2rtc:
- **segrec**: `SEGREC_URL=rtsp://localhost:8554/taman` (di `.env` atau service).
- **pipeline** (`cctv.service`): ganti argumen RTSP ke `rtsp://localhost:8554/taman`.

Efeknya: keduanya baca dari 1 koneksi go2rtc → tak saling ganggu.

## 5. Tambah sumber lain (inti "input apa saja")
Tambah entri di `streams:`:

```yaml
streams:
  taman: rtsp://${NVR_USER}:${NVR_PASS}@${NVR_HOST}:554/Streaming/Channels/202
  garasi: rtsp://${NVR_USER}:${NVR_PASS}@${NVR_HOST}:554/Streaming/Channels/102

  # USB webcam colok langsung (V4L2):
  webcam: ffmpeg:device?video=/dev/video0#video=h264#hardware

  # Node Orange Pi yang MENYAJIKAN RTSP (hub menarik):
  teras_pi: rtsp://<ip-orange-pi>:8554/cam

  # HP / kamera mobil yang MENDORONG (push) RTMP ke hub:
  #   set di app: rtmp://<ip-komputer>:1935/mobil
  mobil: rtmp://localhost:1935/mobil
```
(Untuk push RTMP, aktifkan `rtmp: { listen: ":1935" }` di config.)

## 6. Jadikan service (systemd --user)
`~/.config/systemd/user/go2rtc.service`:
```ini
[Unit]
Description=go2rtc input hub
After=network-online.target

[Service]
WorkingDirectory=%h/go2rtc
EnvironmentFile=%h/Dev/cctv-keamanan-open/.env
ExecStart=%h/go2rtc/go2rtc -config %h/go2rtc/go2rtc.yaml
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```
```bash
systemctl --user daemon-reload && systemctl --user enable --now go2rtc
```

## 7. Latihan (belajar)
1. Tambah `garasi` (ch102) → cek preview di :1984 → `ffprobe rtsp://localhost:8554/garasi`.
2. Colok USB webcam → tambah stream `webcam` → lihat di WebRTC.
3. Ukur latency WebRTC vs RTSP di UI.
4. Arahkan **segrec** ke `rtsp://localhost:8554/taman`, pantau `fps=0` pipeline —
   buktikan hub menghilangkan kontensi.
5. (Lanjut) Siapkan HP sebagai pendorong RTMP → stream `mobil`.

## Catatan integrasi
- go2rtc juga bisa jadi lapisan input untuk **Frigate** (kalau nanti dievaluasi) —
  Frigate memakai go2rtc di dalamnya. Jadi belajar go2rtc = fondasi lintas-pilihan.
- Setelah hub mantap, langkah berikut (Fase 2/3) tinggal menambah kamera & konsumen.
