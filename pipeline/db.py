"""Penyimpanan bersama SQLite (WAL) — KONTRAK antara pipeline inti & service bot.

Pembagian penulis (lihat docs/bot-telegram-spec.md):
  - INTI (run_live.py)  -> tabel `events`         (via EventWriter)
  - BOT  (bot/run_bot)  -> `settings`, `arming_rules`, `notify_log`

Modul ini SENGAJA hanya stdlib (sqlite3/json/time/threading) supaya bisa
diimpor dua-duanya tanpa menyeret dependensi berat pipeline (cv2/ultralytics).
WAL = satu penulis + banyak pembaca lintas-proses aman berbarengan.
"""
import os
import json
import time
import sqlite3
import threading


DB_PATH_DEFAULT = os.environ.get("CCTV_DB", "cctv.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  zone TEXT,
  species TEXT,
  clip TEXT,
  notify INTEGER NOT NULL DEFAULT 0,
  payload TEXT,                      -- JSON event asli (fidelitas caption/summary)
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_notify ON events(notify, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arming_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zones TEXT,                        -- JSON array; NULL/[] = semua zona
  from_hhmm TEXT NOT NULL,
  to_hhmm TEXT NOT NULL,
  notif TEXT NOT NULL,               -- 'aktif' | 'senyap'
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notify_log (
  event_id INTEGER PRIMARY KEY,
  sent_at REAL NOT NULL,
  message_id INTEGER,
  status TEXT NOT NULL               -- sent | skipped_senyap | skipped_filter | skipped_disarmed | failed
);

CREATE TABLE IF NOT EXISTS zone_depth (
  zone TEXT PRIMARY KEY,
  depth INTEGER NOT NULL             -- override ZONE_DEPTH kode; kosong = pakai default kode
);
"""


def connect(path=None, check_same_thread=True):
    con = sqlite3.connect(path or DB_PATH_DEFAULT, timeout=30,
                          check_same_thread=check_same_thread)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def init_db(con):
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('notif_default', 'aktif')")
    con.commit()


# ── penulis event (dipakai INTI) ────────────────────────────────────────────────
class EventWriter:
    """Penulis tabel `events` dari pipeline inti. Aman dipanggil dari beberapa
    thread inti (konsumen klip + worker episode) lewat lock internal
    (check_same_thread=False). Kegagalan tulis DITELAN + dicetak — DB error tak
    boleh mematikan loop deteksi (prinsip sama dgn upload Telegram)."""

    def __init__(self, path=None):
        self.con = connect(path, check_same_thread=False)
        init_db(self.con)
        self.lock = threading.Lock()

    def tulis(self, *, ts, kind, zone=None, species=None, clip=None, notify=0, payload=None):
        try:
            with self.lock:
                self.con.execute(
                    "INSERT INTO events(ts, kind, zone, species, clip, notify, payload, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (ts, kind, zone, species, clip, 1 if notify else 0,
                     json.dumps(payload) if payload is not None else None, time.time()))
                self.con.commit()
        except Exception as e:
            print(f"[ERROR] tulis event ke DB gagal (diabaikan): {kind=} e={e!r}", flush=True)

    def close(self):
        try:
            with self.lock:
                self.con.close()
        except Exception:
            pass


# ── akses config & pengiriman (dipakai BOT) ─────────────────────────────────────
def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()


def list_arming_rules(con, only_enabled=False):
    q = "SELECT * FROM arming_rules"
    if only_enabled:
        q += " WHERE enabled=1"
    q += " ORDER BY id"
    out = []
    for r in con.execute(q):
        out.append({"id": r["id"], "zones": json.loads(r["zones"]) if r["zones"] else [],
                    "from": r["from_hhmm"], "to": r["to_hhmm"],
                    "notif": r["notif"], "enabled": bool(r["enabled"])})
    return out


def add_arming_rule(con, zones, from_hhmm, to_hhmm, notif):
    cur = con.execute(
        "INSERT INTO arming_rules(zones, from_hhmm, to_hhmm, notif, enabled) VALUES(?,?,?,?,1)",
        (json.dumps(zones) if zones else None, from_hhmm, to_hhmm, notif))
    con.commit()
    return cur.lastrowid


def delete_arming_rule(con, rule_id):
    con.execute("DELETE FROM arming_rules WHERE id=?", (rule_id,))
    con.commit()


def set_rule_enabled(con, rule_id, enabled):
    con.execute("UPDATE arming_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
    con.commit()


def unsent_events(con, limit=20):
    """Event notify=1 yang BELUM ada di notify_log (urut terlama dulu).
    payload di-parse jadi dict siap-format."""
    rows = con.execute(
        "SELECT e.* FROM events e "
        "LEFT JOIN notify_log n ON n.event_id = e.id "
        "WHERE e.notify=1 AND n.event_id IS NULL "
        "ORDER BY e.id LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "zone": r["zone"],
                    "species": r["species"], "clip": r["clip"],
                    "payload": json.loads(r["payload"]) if r["payload"] else {}})
    return out


def log_send(con, event_id, status, message_id=None):
    con.execute("INSERT OR REPLACE INTO notify_log(event_id, sent_at, message_id, status) "
                "VALUES(?,?,?,?)", (event_id, time.time(), message_id, status))
    con.commit()


def summary_since(con, since_ts):
    """Ringkasan agregat sejak `since_ts` (jumlah per kind+zone)."""
    rows = con.execute(
        "SELECT kind, zone, COUNT(*) n FROM events WHERE ts>=? GROUP BY kind, zone ORDER BY n DESC",
        (since_ts,)).fetchall()
    return [{"kind": r["kind"], "zone": r["zone"], "n": r["n"]} for r in rows]


def count_since(con, since_ts):
    return con.execute("SELECT COUNT(*) n FROM events WHERE ts>=?", (since_ts,)).fetchone()["n"]


def busiest_hour_since(con, since_ts):
    """(jam_lokal 'HH', jumlah) tersibuk sejak since_ts, atau None."""
    r = con.execute(
        "SELECT strftime('%H', ts, 'unixepoch', 'localtime') jam, COUNT(*) n "
        "FROM events WHERE ts>=? GROUP BY jam ORDER BY n DESC LIMIT 1", (since_ts,)).fetchone()
    return (r["jam"], r["n"]) if r else None


# ── zone-depth (override peta kedalaman kode; dibaca INTI, ditulis BOT) ──────────
def get_zone_depth(con):
    return {r["zone"]: r["depth"] for r in con.execute("SELECT zone, depth FROM zone_depth")}


def set_zone_depth(con, zone, depth):
    con.execute("INSERT INTO zone_depth(zone, depth) VALUES(?, ?) "
                "ON CONFLICT(zone) DO UPDATE SET depth=excluded.depth", (zone, int(depth)))
    con.commit()


def all_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}
