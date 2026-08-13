# cctv-keamanan

Pipeline analitik CCTV rumah: **deteksi → tracking → zona → kejadian**. Dari aliran RTSP,
ia mengenali orang (& kucing), menentukan mereka di zona mana (pintu, teras, taman, dst.),
lalu memancarkan kejadian bermakna — **masuk/keluar rumah**, **masuk/keluar property**,
**berlama-lama (loiter)** — merekam klip, dan mengirim notifikasi Telegram. Ada juga
**web viewer** untuk menelusuri kejadian & memutar footage.

> Proyek pribadi yang di-open-source-kan. Footage, kredensial, dan data aktivitas
> **tidak** disertakan (lihat [Privasi](#privasi)).

## Fitur

- **Deteksi + tracking** (Ultralytics YOLO + BoT-SORT/ReID), anchor kaki (`BOTTOM_CENTER`).
- **Zona berbasis poligon** (`zones*.json`) dengan sumbu *kedalaman* luar→dalam.
- **Arah masuk/keluar rumah** dari **lintasan** track relatif pintu (tahan kaki-jitter & anak berlari).
- **Arah masuk/keluar property** dari lahir/mati track di tepi (jalanan tak teramati).
- **Episode scene-level**: SATU video utuh per kunjungan (dari muncul s.d. hilang),
  dibingkai *by-zone* (kebal kedip ID), ditulis streaming.
- **Klip & snapshot** per kejadian, **notifikasi Telegram**, **jadwal arming** (mode senyap).
- **Web viewer**: log + pemutar + pencarian + tarik footage NVR + mode Episode.

## Arsitektur (stage pipeline)

```
RTSP ─▶ DetectorTracker ─▶ Occupancy(zona) ─┬─▶ RuleEngine ─▶ event (close/loiter/masuk/keluar)
                                            └─▶ SceneEpisode ─▶ EpisodeRecorder (video utuh)
                                                       │
                                    ClipBuffer/ClipRecorder ─▶ klip + Telegram
```

Logika domain (murni, teruji) ada di `pipeline/live.py`; orkestrasi I/O di
`pipeline/run_live.py`. Semua tracker punya test nol-deps (`pipeline/test_*.py`).

## Menjalankan

Butuh Python 3.1x + [uv](https://docs.astral.sh/uv/), GPU untuk `h264_nvenc` (opsional).

```bash
uv sync
# unduh bobot model YOLO ke root (mis. yolo26l.pt), lalu:
uv run --env-file .env pipeline/run_live.py "$RTSP_URL" \
    --model yolo26l.pt --conf 0.15 --zone-file zones-102.json
```

`.env` (tak disertakan) memuat rahasia:

```
TG_TOKEN=...            # bot Telegram
TG_CHAT_ID=...
NVR_HOST=...            # opsional: tarik footage penuh via viewer
NVR_USER=...
NVR_PASS=...
```

Web viewer: `uv run pipeline/viewer.py` (baca `.env` untuk NVR).

## Zona

`zones-102.json` = poligon pada geometri **640×360** (dipakai produksi); `zones.json` =
resolusi penuh. Konversi antar-resolusi: `pipeline/scale_zones.py`. Menggambar/menurunkan
poligon dari data: lihat `debug-zona/` (kumpul titik kaki → peta kepadatan → poligon).

Sumbu kedalaman (di `pipeline/live.py`, satu sumber `ZONE_DEPTH`):
`luar(0) → tepi properti(1) → halaman/teras(2) → pintu/rumah(3)`.

## Uji

```bash
for t in pipeline/test_*.py; do uv run "$t"; done
```

## Privasi

Repo ini **tidak** memuat: footage (`out/`, `*.mp4`, `*.jpg`), kredensial (`.env`),
maupun data aktivitas (`events-live.jsonl`, `episodes-live.jsonl`). `zones*.json` hanya
koordinat poligon (bukan citra). Sesuaikan sendiri untuk kameramu.

## Lisensi

TODO: pilih lisensi (mis. MIT).
