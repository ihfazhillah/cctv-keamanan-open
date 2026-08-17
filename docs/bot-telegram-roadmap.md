# Roadmap: kelola CCTV dari Telegram — mudah vs berat

Pendamping `bot-telegram-spec.md`. Garis pemisah utama:

> **Notifikasi/presentasi = urusan bot** (mudah, inti tak disentuh).
> **Deteksi = urusan inti** (butuh inti *membaca* config dari DB — infra Paket 2).

Status per 2026-08-15: **Paket 1 & 2 terimplementasi** (lihat §Implementasi).

---

## 🟢 Tier 1 — murni bot (nol perubahan inti)  · SUDAH (Paket 1)

- **Ringkasan kaya** — periode Hari ini / 24 jam / 7 hari, total, per-jenis, zona
  teratas, jam tersibuk. (`/ringkasan` + tombol periode)
- **Filter jenis notif** — toggle kirim: close / loiter / masuk-keluar / kucing.
- **Armed master switch** — DISARM = bot tahan semua notif (event tetap tercatat).
- **Jendela arming per-zona** — alur tambah kini punya langkah pilih zona.
- Sisa ide Tier 1 (belum): snooze cepat "senyap 1 jam", kirim-ulang klip tertentu.

## 🟡 Tier 2 — inti baca config dari DB  · SUDAH (infra + kenop awal)

Infra: `ConfigPoll` di `run_live.py` poll `settings`/`zone_depth` tiap ~2 dtk,
terap live (semua try/except; config rusak tak mematikan loop).

- **conf detektor** — set dari chat, berlaku live (dibaca per-frame).
- **loiter_s** — set dari chat (terap ke `engine.ep_tracker` & `cat_engine`).
- **zone-depth** — editor tombol; override `live.ZONE_DEPTH` in-place (menyebar ke
  `depth_of`/`TrackPassageTracker`/`SceneEpisode`). ⚠️ menyetir kebenaran
  MASUK/KELUAR — lihat validasi pintu.
- Belum: exit-hysteresis / min-presence (pola sama, tinggal tambah kenop).

## 🔴 Tier 3 — berat / jalur lain

- **Geometri poligon via chat** — UX buruk; pakai **viewer web** yang sudah ada.
- **Tipe deteksi BARU dari chat** (mis. "≥2 orang di taman >5 mnt setelah 22:00")
  — butuh **DSL/rule-engine data-driven**; tracker kini kode Python murni.
- **Rule berbasis identitas** ("orang tak dikenal") — tergantung ReID (Lesson 0011/0012).

---

## Audio + video: teknik menggabungkan

Masalah: pipeline membaca RTSP via **OpenCV** yang **membuang audio** — hanya
frame video. Jadi klip sekarang bisu. Tiga jalur:

1. **Re-pull segmen A/V via ffmpeg saat trigger** (deteksi memutuskan KAPAN,
   ffmpeg mengambil jendela [t0,t1] lengkap audio dari RTSP/NVR). Viewer sudah
   punya pola ini (`nvr_grab`, playback Hikvision). Bersih, kualitas asli.
2. **Rekam kontinu paralel + mux** — proses ffmpeg terpisah merekam A/V terus;
   saat klip dipotong, mux audio irisan waktu ke video
   (`ffmpeg -i vid -i aud -c copy -map 0:v -map 1:a`). Perlu sinkron timestamp.
3. **Segment muxer sebagai sumber klip** (lihat bawah) — paling elegan: segmen
   sudah membawa audio, klip = potong-sambung segmen.

Rekomendasi: **(3)**, karena sekaligus menjawab pertanyaan streaming-writer.

## Streaming writer untuk semua (ganti buffer RAM)

Sekarang `ClipBuffer`/`PendingNotifier` menahan **frame numpy di RAM** lalu encode
saat trigger (`EpisodeRecorder` sudah streaming, tapi tetap dari OpenCV, bisu).

Usulan: **rekaman berbasis segmen ffmpeg** sebagai satu-satunya sumber klip.
- Satu proses ffmpeg merekam RTSP → **segmen bergulir di disk** (mis. `.ts` 2–4
  dtk, `-strftime` untuk nama berstempel waktu, buang segmen lama). Near-zero RAM,
  **membawa audio**, dan **tanpa re-encode** (copy codec kamera h264/h265).
- Deteksi (OpenCV) jalan paralel hanya untuk logika.
- Klip untuk [t0,t1] = `ffmpeg -ss t0 -to t1` sambung segmen yang menutupi jendela
  (copy) → klip A/V presisi. **Pre-roll otomatis** karena segmen masa lalu sudah
  ada di disk.

Untung: audio ikut · RAM turun drastis · hapus re-encode per klip · pre/post-roll
gratis. Tebusan: kelola siklus hidup + pembersihan segmenter; potong copy menempel
ke keyframe (±1 GOP) — cukup untuk klip keamanan, atau re-encode hanya klipnya bila
butuh presisi frame. Anotasi kotak hilang di klip (kini klip memang tak beranotasi,
jadi tak masalah).

**Sifat perubahan:** ini mengganti mekanisme perekam inti — lebih besar & lebih
berisiko dari Paket 1/2 (menyentuh service keamanan yang live). Diusulkan sebagai
**track terpisah**, di-flag & diuji berdampingan sebelum cutover, seperti
`TG_VIA_BOT`.

---

## Implementasi (Paket 1 & 2)

Berkas: `bot/run_bot.py` (menu berlapis + filter + ringkasan + editor config),
`pipeline/db.py` (tabel `zone_depth`, helper filter/summary), `pipeline/run_live.py`
(`ConfigPoll`). Setting yg dipakai: `notif_default`, `armed`, `send_close/loiter/
transit/kucing`, `det_conf`, `loiter_s`, tabel `arming_rules` & `zone_depth`.
Semua diedit dari `/menu`.
