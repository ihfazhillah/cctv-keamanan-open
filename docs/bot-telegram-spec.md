# Spec: Bot Telegram Interaktif (service terpisah)

Status: **v1 deployed; Paket 1 & 2 terimplementasi** (filter, ringkasan kaya, per-zona, config-poll inti) · Roadmap: `bot-telegram-roadmap.md` · Terakhir diperbarui: 2026-08-15

## 1. Tujuan

Mengubah Telegram dari sekadar *notifier* satu-arah menjadi **bot dua-arah** yang
bisa diatur dari chat, berjalan sebagai **service terpisah** dari pipeline inti,
berbagi data lewat **SQLite**.

Kemampuan target (bertahap):
1. **Notifikasi arming** — aktif/nonaktifkan notifikasi + jadwal senyap. ← **v1**
2. Ringkasan harian (query on-demand). ← nanti
3. Rule management CRUD (lapisan berbasis-data). ← nanti

UX memakai **tombol inline / menu**, bukan hanya perintah teks.

## 2. Prinsip arsitektur

- **Inti bodoh soal notifikasi.** `run_live.py` tidak tahu apa-apa tentang
  Telegram, arming, atau format pesan. Inti hanya: *"ini event, ini klipnya,
  `notify=1` bila menurut rule-deteksi layak diberitahu"* → tulis ke DB.
- **Bot = satu-satunya gateway Telegram.** Bot memutuskan **kapan** benar-benar
  kirim (menerapkan jadwal arming), **cara memformat** caption, dan menangani
  semua interaksi masuk.
- **Berbagi lewat SQLite (mode WAL).** Satu penulis + banyak pembaca lintas-proses
  aman berbarengan. Inti = penulis tabel `events`; bot = penulis tabel config &
  `notify_log`. Tidak ada broker/HTTP — pola shared-file yang sudah dipakai
  (`arming.json`), dipindah ke DB.
- **Polling `getUpdates`** untuk terima perintah (belum ada IP publik → tanpa
  webhook).
- **Perubahan inti minimal & aman** di balik flag; JSONL lama tetap ditulis.

```
   ┌─────────────────────────────────┐
   │  run_live.py  (INTI)            │  cctv.service (peran tak berubah)
   │  detektor + rule engine        │
   │  ▸ tulis event + episode  ─────┼──┐
   │  ▸ tulis klip → out/live/      │  │
   └─────────────────────────────────┘  │      cctv.db (SQLite WAL)
                                         │      ┌───────────────────────────┐
                                         ├─────▶│ events            (tulis: INTI) │
                                         │      │ settings, arming_rules (tulis: BOT) │
   ┌─────────────────────────────────┐  │      │ notify_log        (tulis: BOT)  │
   │  cctv-bot  (SERVICE BARU)  bot/ │◀─┘      └───────────────────────────┘
   │  ▸ thread pengirim: poll events │
   │    notify=1 → arming → kirim/skip│         + out/live/*.mp4  (dibaca BOT)
   │  ▸ thread telebot: menu tombol  │
   │    arming (aktif/senyap/jadwal)  │
   └─────────────────────────────────┘
```

## 3. Kontrak data (skema `cctv.db`)

```sql
-- ditulis INTI, dibaca BOT
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,                   -- epoch mulai event
  kind TEXT NOT NULL,                 -- close | loiter | MASUK rumah | KELUAR rumah
                                      --   | MASUK property | KELUAR property | episode
  zone TEXT,                          -- nullable
  species TEXT,                       -- opsional (mis. "kucing")
  arah TEXT,                          -- utk episode: masuk|keluar|lewat
  clip TEXT,                          -- path klip relatif (out/live/...), nullable
  notify INTEGER NOT NULL DEFAULT 0,  -- 1 = layak diberitahu (= yg dulu di-upload inti)
  created_at REAL NOT NULL
);
CREATE INDEX idx_events_notify ON events(notify, id);

-- ditulis BOT (arming). Inti tak menyentuh.
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);  -- baris awal: notif_default = 'aktif'

CREATE TABLE arming_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zones TEXT,                 -- JSON array nama zona; NULL/[] = semua zona
  from_hhmm TEXT NOT NULL,    -- '22:00'
  to_hhmm TEXT NOT NULL,      -- '06:00'  (dukung lintas-malam)
  notif TEXT NOT NULL,        -- 'aktif' | 'senyap'
  enabled INTEGER NOT NULL DEFAULT 1
);

-- ditulis BOT: cegah dobel-kirim / audit
CREATE TABLE notify_log (
  event_id INTEGER PRIMARY KEY,
  sent_at REAL NOT NULL,
  message_id INTEGER,
  status TEXT NOT NULL        -- sent | skipped_senyap | failed
);
```

Catatan:
- **Caption tidak disimpan** — presentasi urusan bot. `ZONE_EMOJI` (kini di
  `run_live.py`) diangkat ke bot; caption dibentuk dari `kind/zone/species/arah`.
- **`notify`** = himpunan event yang *saat ini* di-upload inti (klip terkirim).
  Event yang cuma dicatat (mis. `close` kucing) → `notify=0`. Pemetaan pasti
  ditentukan saat implementasi dengan membaca `ClipRecorder`.
- **Path klip** sudah diketahui inti saat menulis (baru selesai encode).

## 4. Semantik arming (diangkat dari `live.py`)

Keputusan kirim untuk satu event `notify=1` pada waktu `ts`:
1. Mulai dari `settings.notif_default`.
2. Terapkan `arming_rules` yang `enabled=1`, **last-match-wins**, cocok bila:
   jam `ts` ada di jendela `[from_hhmm, to_hhmm)` (dukung lintas-malam) **dan**
   (`zones` kosong **atau** `event.zone` ∈ `zones`).
3. Hasil `senyap` → catat `notify_log(status=skipped_senyap)`, tidak kirim.
   Hasil `aktif` → kirim, catat `status=sent` + `message_id`.

Logika murni `notif_aktif()` / `in_window()` di `live.py` dipakai ulang (di-port).

## 5. Perubahan pada inti (`run_live.py`) — minimal & ber-flag

- Tambah penulis DB (modul kecil, mis. `pipeline/db.py`): buka `cctv.db` (WAL),
  fungsi `tulis_event(...)`.
- Di titik `ClipRecorder._log()` / `EpisodeRecorder._finalize()` (yang sudah
  membentuk dict `ev` + path klip): panggil `tulis_event(...)` dengan `notify`.
- **Flag `TG_VIA_BOT`** (env):
  - `TG_VIA_BOT=1` → inti **berhenti** memanggil `self.tg.*`; hanya tulis DB.
  - unset/`0` → perilaku lama utuh (inti kirim sendiri). Default aman.
- **JSONL tetap ditulis** apa pun flag-nya (log durable; `viewer.py` tetap jalan).

Cutover: uji bot penuh dengan `TG_VIA_BOT` belum diset (inti masih kirim, bot
menulis `notify_log` "kering" untuk verifikasi), lalu jentik `TG_VIA_BOT=1` saat
yakin.

## 6. Service bot (`bot/` di dalam `-open`)

- Library: **`pyTelegramBotAPI` (telebot)** — offset polling otomatis, decorator
  `callback_query_handler`, `InlineKeyboardMarkup` untuk menu. `uv add
  pytelegrambotapi`.
- Kredensial dari `.env` yang sama (`TG_TOKEN`, `TG_CHAT_ID`).
- Dua thread:
  - **Pengirim**: loop poll `SELECT ... FROM events WHERE notify=1 AND id NOT IN
    (SELECT event_id FROM notify_log)` (interval ~2–3 dtk) → terapkan arming →
    kirim `sendMessage/sendPhoto/sendVideo` (media dari `clip`) → tulis
    `notify_log`.
  - **telebot**: `infinity_polling()` untuk perintah & tombol.
- systemd user unit **`cctv-bot.service`** (`WorkingDirectory=-open`,
  `ExecStart=uv run --env-file .env bot/...`, `Restart=always`), aman jalan
  berbarengan dengan `cctv.service`.

### UX v1 (menu tombol arming)
- `/start` atau `/menu` → papan inline:
  - **Status**: default aktif/senyap sekarang + ringkas jendela aktif.
  - Tombol **Aktifkan semua** / **Senyapkan semua** (set `notif_default`).
  - **Jadwal** → daftar `arming_rules` dengan tombol hapus per baris + tombol
    "Tambah jendela" (alur tanya jam mulai → jam selesai → zona → aktif/senyap).
  - Perubahan langsung tulis DB; berlaku untuk event berikutnya tanpa restart.

## 7. Lingkup v1 (yang dikerjakan lebih dulu)

**Termasuk:** skema `cctv.db`, penulis-DB ber-flag di inti, service `bot/`,
thread pengirim (gateway), menu tombol arming (default + jadwal CRUD),
`cctv-bot.service`.

**Belum:** ringkasan harian, CRUD threshold/zone-depth, edit geometri, DSL rule.

## 8. Roadmap sesudah v1

- **Mudah (lapisan berbasis-data):** ringkasan harian dari tabel `events`;
  CRUD `arming_rules` lebih kaya; jadikan `settings` menampung threshold
  (`loiter_s`, `conf`) + `zone_depth` sebagai tabel → inti poll & reload.
- **Sedang:** edit peta kedalaman zona (ubah logika MASUK/KELUAR tanpa sentuh kode).
- **Berat / nanti:** edit geometri poligon via chat (UX buruk); **DSL rule** untuk
  membuat *tipe deteksi baru* dari chat (butuh rule-engine tersendiri) — logika
  tracker (`EpisodeTracker`, `TrackPassageTracker`) saat ini kode Python murni.

Daftar rinci "possible vs tidak" akan disusun setelah v1 berjalan.

## 8b. Implementasi v1 (berkas & cara pakai)

Berkas yang ditambahkan/diubah:
- `pipeline/db.py` — modul penyimpanan bersama (skema, `EventWriter`, helper bot).
- `pipeline/run_live.py` — `EventWriter` + flag `TG_VIA_BOT` (perubahan aman, ber-flag).
- `bot/run_bot.py` — service bot (thread pengirim + menu telebot).
- `bot/arming.py`, `bot/format.py` — evaluasi arming & caption (mandiri, tanpa impor pipeline berat).
- `bot/cctv-bot.service` — unit systemd user.
- dependensi: `pytelegrambotapi`.

**Uji lokal (tanpa deploy):**
```bash
# 1) jalankan bot (baca .env yg sama)
uv run --env-file .env bot/run_bot.py
# 2) di Telegram: /menu -> atur default aktif/senyap, /menu -> Jadwal -> Tambah jendela
#    /ringkasan -> agregat 24 jam
```

**Aktifkan gateway (cutover):**
1. Uji dulu: biarkan `TG_VIA_BOT` belum diset → inti tetap kirim seperti biasa,
   sambil bot berjalan (bisa dobel-kirim saat uji; itu wajar). Pastikan bot
   mengirim dengan benar.
2. Flip: tambahkan `TG_VIA_BOT=1` ke `.env`, `systemctl --user restart cctv`.
   Sejak itu inti diam; hanya bot yang mengirim + menerapkan arming.
3. Pasang service bot:
   ```bash
   ln -s ~/Dev/cctv-keamanan-open/bot/cctv-bot.service \
         ~/.config/systemd/user/cctv-bot.service
   systemctl --user daemon-reload && systemctl --user enable --now cctv-bot
   ```

Catatan: `arming.json` lama kini tak dipakai bot (arming pindah ke DB). Ia masih
dihormati inti HANYA di jalur non-gateway (`TG_VIA_BOT` belum diset).

## 9. Keputusan yang sudah dikunci

- Berbagi data: **SQLite (WAL)**, bukan JSONL/HTTP.
- Bot = **gateway Telegram tunggal**; inti tak tahu-menahu soal pengiriman.
- **Polling** `getUpdates` (tanpa webhook).
- Library **telebot** (bukan `requests` mentah) demi tombol/menu.
- Lokasi: folder **`bot/`** di repo `-open`; service systemd sendiri.
- Mulai dari **arming saja**, sisanya bertahap.
```
