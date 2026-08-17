# Rencana: CCTV Hibrida — Lab Belajar + NVR Lokal

Status: rancangan · 2026-08-17

## Prinsip
- **Pisahkan "permukaan belajar" dari "komoditas".**
  - *Belajar (custom, dipertahankan):* alur deteksi, ReID, custom model, action
    recognition ("orang sedang ngapain"), rule.
  - *Komoditas (pakai yang ada / bangun seperlunya):* input kamera, config, zona,
    rekam, playback, retensi, UI.
- **`go2rtc` = gerbang input universal.** Semua sumber (IP cam, USB, node Orange Pi,
  kamera mobil) dinormalkan jadi RTSP di satu tempat. "Tambah kamera" = edit config.
- **Otak di komputer (ber-GPU).** Orange Pi/SBC hanya *camera node* (tangkap → kirim),
  tak menghitung.
- **Satu rekaman penuh = sumber tunggal.** Klip event = potongan dari arsip segrec.

## Arsitektur target
```
   sumber apa saja: IP cam · USB webcam · Orange Pi node · kamera mobil(RTMP)
                                  │
                        ┌─────────▼─────────┐
                        │  go2rtc (HUB)     │  gerbang input universal
                        └───┬───────────┬───┘
             (RTSP norm.)   │           │
        ┌──────────────────▼──┐    ┌───▼──────────────────────┐
        │ PIPELINE (LAB)      │    │ segrec (ARSIP / NVR lokal)│
        │ detect·ReID·model·  │    │ rekam penuh semua kamera  │
        │ action·rule → BOT TG│    │ arsip bergulir 50GB       │
        └─────────┬───────────┘    └───────────┬──────────────┘
                  │ event → cctv.db             │ segmen
                  └──────────────┬──────────────┘
                                 ▼
                bot potong klip event dari arsip → Telegram
                viewer.py: telusur arsip + klip event   [+ opsional Home Assistant]
```

## Status sekarang (sudah jadi)
- ✅ Pipeline deteksi + rule (masuk/keluar, loiter, episode) — jalan.
- ✅ Bot Telegram dua-arah (arming, filter, ringkasan, config live) via SQLite bersama.
- ✅ segrec: rekam segmen A/V (audio), arsip bergulir **50GB / subfolder per jam**.
- ✅ Cutover: klip Telegram dipotong dari segmen (**beraudio + mulus**), sumber NVR,
  0 stall pipeline. Terkonfirmasi produksi.
- ✅ viewer.py: web viewer klip event + search + NVR-grab + mode Episode.

## Fase

### Fase 0 — Rapikan fondasi (cepat, tutup hutang)
- **Notif dari EPISODE** (bukan transit) — episode andal menangkap masuk/keluar yang
  transit sering lewatkan (mis. 07:16). Menutup akar keluhan MASUK/KELUAR rumah.
- **Matikan encode-klip pipeline** — klip kini dari segmen → hilang micro-stall +
  hemat CPU; `out/live` jadi usang.
- **Retensi `out/live`** — 15GB tak terbendung; beri cap/umur atau arsipkan.

### Fase 1 — Gerbang input universal (go2rtc)
- Pasang `go2rtc` hub di komputer (shadow dulu).
- Arahkan pipeline + segrec baca **dari go2rtc**, bukan langsung RTSP kamera.
- Tambah **kamera kedua (garasi)**.
- Dokumen cara tambah sumber: USB langsung, node Orange Pi (push/pull RTSP), kamera mobil.

### Fase 2 — Arsip jadi "NVR" (viewer)
- Kembangkan `viewer.py`: **telusur arsip segmen per waktu** (putar/unduh per jam/rentang)
  + gabung dgn klip event kita dalam satu tampilan.
- (Opsional) **Home Assistant** sebagai dashboard ramah di depan: live (go2rtc/WebRTC) +
  panel klip kita.

### Fase 3 — Multi-kamera & edge node
- Orange Pi / USB / kamera mobil sebagai *camera node* → go2rtc.
- segrec rekam semua kamera (subfolder per-kamera).

### Fase 4 — Track belajar ML (paralel; INTI minat)
- **ReID**: eksperimen model, ukur ID-switch/IDF1 (Lesson 0011/0012).
- **Custom model**: swap/latih YOLO sendiri.
- **Action recognition** ("orang ngapani"): pose (YOLO-pose/MMPose) → aksi
  (ST-GCN/PoseC3D di skeleton, atau SlowFast). Ranah murni custom.
- **Rule berbasis identitas/aktivitas** (mis. "orang tak dikenal", "memanjat pagar").

## Keputusan yang sudah dikunci
- Pendekatan **hibrida** (custom = lab, komoditas = pakai yang ada).
- **go2rtc** sebagai gerbang input tunggal.
- **Orange Pi = camera node saja** (otak di komputer).
- **viewer.py** sebagai wadah utama klip/arsip; UI jadi (Frigate/Shinobi) tak dipakai
  sebagai wadah data custom karena mengelola event-nya sendiri.

## Langkah pertama yang disarankan
**Fase 0** dulu (cepat & berdampak): notif-dari-episode + matikan encode pipeline +
retensi out/live. Lalu **Fase 1**: pasang go2rtc (membuka semua fleksibilitas input).
