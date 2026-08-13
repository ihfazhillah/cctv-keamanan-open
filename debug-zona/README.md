# Debug Zona — menggambar & memvalidasi zona dari data

Alur berulang untuk **mendiagnosis masalah zona** (mis. "dari taman ke teras dianggap
masuk rumah") dan **menggambar zona dari perilaku yang direkam kamera**, bukan dari tebakan.
Materi konsep lengkap (state machine, birth/death, peta kepadatan) ada di repo belajar
privat: `lessons/0010-menggambar-zona-dari-data.html`.

Semua skrip meniru produksi: **640×360, yolo26l @ conf 0.15, botsort_reid, anchor kaki
(BOTTOM_CENTER), `zones-102.json`**. Jalankan dengan `.venv/bin/python`.

## Prinsip

1. **Titik kaki** = satuan ukur. Zona ditentukan oleh satu jangkar (tengah-bawah kotak =
   kaki), bukan seluruh kotak. Itu petak lantai tempat orang berdiri.
2. **MUNCUL (lahir) & HILANG (mati)** menandai batas ke wilayah **tak-teramati**
   (jalanan; interior rumah). Kamera tak melihat ke dalam, jadi arah disimpulkan dari
   perilaku di batas.
3. **Kepadatan** mengubah keramaian titik menjadi bentuk: wilayah terpanas = tempat nyata
   (pintu, tepi). Ada bisa >1 titik panas — isolasi yang benar (ROI).
4. **Angka cakupan** yang memutuskan, bukan "kelihatan pas".

## Langkah

### 1. Kumpulkan titik
```bash
.venv/bin/python debug-zona/1_kumpul_titik.py out/live/klip_*rumah*.mp4
```
→ `out/points.json` (semua titik lahir+mati) & `out/birthdeath.jpg` (hijau=muncul,
oranye=hilang). Gumpalan padat = pintu/tepi yang sebenarnya dipakai.

### 2. Gambar poligon dari kepadatan
```bash
.venv/bin/python debug-zona/2_gambar_poligon.py            # ROI default = pintu kiri
.venv/bin/python debug-zona/2_gambar_poligon.py 0 120 150 240   # ROI custom (x1 y1 x2 y2)
```
→ cetak koordinat poligon + `out/poligon.jpg` (magenta=lama, putih=baru, kuning=ROI) +
cakupan titik LAMA vs BARU. Salin poligon ke `zones-102.json` (dan skala ×3.6 ke
`zones.json` untuk resolusi penuh). Semakin banyak klip di langkah 1, semakin andal.

### 3. Uji end-to-end sebelum deploy
```bash
TG_TOKEN=x TG_CHAT_ID=x .venv/bin/python debug-zona/3_uji_e2e.py out/live/klip_keluar_rumah_*.mp4
```
→ jalankan pipeline nyata (deteksi→zona→RuleEngine), cetak event masuk/keluar rumah.
Bandingkan dengan yang seharusnya. Env: `MINPRES` (ambang umur track), `ZONEF` (file zona).

## Catatan penting yang ditemukan lewat alur ini

- **`pintu` harus MENANG di tumpang-tindih dengan `teras`.** Kaki di ambang jatuh di irisan
  kedua poligon; bila `teras` menang, pintu tak pernah teregister. `pipeline/run_live.py`
  `muat_zone` menaruh `pintu` paling akhir agar menang.
- **Arah masuk/keluar dari LINTASAN**, bukan lahir/mati mentah: datang-dari-halaman lalu
  lenyap di pintu = MASUK; muncul-di-pintu lalu ke-halaman = KELUAR. Menjejak pintu lalu
  balik = bukan event. Ini menangani **anak yang berlari** dan tahan kaki-jitter di batas.
- **Label event lama BUKAN ground-truth** — itu output aturan yang sedang diperbaiki.
  Validasi recall butuh set berlabel-tangan (tonton klip, tandai benar masuk/keluar/nunggu).
- **Batas deteksi**: anak sangat mungil & cepat di tepi pintu kadang tak terdeteksi sama
  sekali — itu soal model/oklusi, bukan aturan.
