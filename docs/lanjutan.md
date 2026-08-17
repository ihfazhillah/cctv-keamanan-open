# Catatan Lanjutan — CCTV (per 2026-08-17)

Ringkasan status + cara melanjutkan besok. (Detail teknis ada di git log & kode;
ini peta cepat.)

## Kondisi sistem sekarang (semua jalan)

**Satu service utama = supervisor.** `cctv.service` menjalankan
`pipeline/run_supervisor.py` yang membaca `cameras.json` dan spawn sub-proses per
kamera (isolasi, reuse kode apa adanya):

```
cctv.service  (supervisor)
  ├── run_live.py     ← kamera "taman-penuh" (analitik penuh, yolo26l)   [per kamera]
  ├── run_garasi.py   ← semua kamera "garasi-ringan" (deteksi orang, yolo11s dibagi)
  └── run_segrec.py   ← rekam per kamera -> out/segments/<nama>/          [rekam:true]
```

**Tambah/ubah kamera = edit `cameras.json`** (lewat viewer "📷 Kamera" atau Telegram)
→ supervisor start/stop/restart sub-proses **LIVE, tanpa systemctl**.

Service lain (beda urusan, terpisah): `cctv-bot` (Telegram), `cctv-retensi.timer` +
`cctv-retensi-segclip.timer` (bersih-bersih). `go2rtc.service` = gerbang input
(`localhost:8554/<nama>`, config di `/home/ihf/Dev/gortc/go2rtc.yaml`).

**Kamera aktif:** `taman` (analitik penuh + rekam), `garasi` (deteksi orang malam
22:00–04:00 + rekam). Keduanya live + terekam + notif.

## Cara operasional cepat
- **Buka viewer:** klik ikon **CCTV Viewer** (menu aplikasi) → jendela app. Tab:
  **Event · Episode · Arsip · Live** + tombol **📷 Kamera** & **⚙ Jadwal**.
  (Ikon menjalankan `desktop/launch.sh` = `uv run pipeline/viewer.py`.)
- **Live** = grid semua kamera (video-call, lebar penuh, auto √n kolom; klik ⛶ = layar penuh).
- **Arsip** = telusur rekaman per kamera (pemilih kamera) → HLS jam penuh / potong rentang.
- **Atur jadwal/kamera:** viewer "📷 Kamera" ATAU Telegram `/menu` → "📷 Garasi".
- **Cek sehat:** `journalctl --user -u cctv -f` (semua anak) ·
  `out/segrec-health-<kamera>.json` (rekam) · `pgrep -af run_live\|run_garasi\|run_segrec`.

## Roadmap — posisi
- ✅ **Fase 0** fondasi (notif-episode, matikan encode, retensi)
- ✅ **Fase 1** go2rtc + kamera garasi
- ✅ **Fase 2** viewer NVR (Arsip + HLS + "buka di arsip" + **Live**)
- ✅ **Fase 3 (rekam multi-kamera)** — segrec per kamera + Arsip camera-aware
- ⏳ **Fase 3 sisa:** edge node (Orange Pi / USB / kamera mobil → go2rtc) — butuh hardware
- ⏳ **Fase 4 (INTI MINAT — ML):** action recognition "orang ngapain" (pose → aksi),
  ReID (ukur ID-switch), custom model, rule berbasis identitas/aktivitas

## Selanjutnya (pilih besok)
1. **Fase 4 ML** (paling menarik, tujuan proyek): mulai dari *action recognition*
   (YOLO-pose → aksi: berdiri/jalan/memanjat) atau eksperimen ReID. Ranah lab custom.
2. **Edge node** (Fase 3): tambah sumber non-NVR ke go2rtc (butuh perangkat).
3. **Polish** (opsional kecil): snapshot di notif garasi; klip video di notif garasi
   (sekarang teks — arsip garasi sudah ada, tinggal potong dari `out/segments/garasi`);
   heartbeat/health garasi; panel "📷 Kamera" belum bisa edit `zone_file/model/conf/rekam_gb`
   (dipertahankan saat simpan, tapi belum ada field editnya).

## Catatan teknis penting
- `cameras.json` & `.env` **gitignored** (jadwal = privasi; kredensial). go2rtc.yaml di
  luar repo. Config kamera = **nama stream go2rtc** (bukan URL berkredensial).
- Bug segrec diperbaiki: watchdog stall kini punya *grace period* → pulih dari jeda
  (dulu bisa stuck berjam-jam setelah restart go2rtc).
- Bot potong klip **taman** dari `out/segments/taman` (`.env: SEG_DIR`). Notif **garasi**
  masih teks (belum potong klip — kandidat polish).
- Repo ini publik (`github.com/ihfazhillah/cctv-keamanan-open`) — sudah discrub dari
  IP/username; jangan commit kredensial/IP.
