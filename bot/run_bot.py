"""Service bot Telegram (terpisah dari pipeline inti).

Gateway Telegram TUNGGAL bila inti dijalankan dgn TG_VIA_BOT=1: inti hanya
menulis event ke SQLite bersama; bot ini yang mengirim + menerapkan
arming/filter + melayani menu tombol (kelola arming, filter, & config deteksi).

Dua thread:
  - PENGIRIM : poll event notify=1 belum terkirim -> armed? filter jenis?
               jadwal arming? -> kirim video/teks -> catat notify_log.
  - TELEBOT  : infinity_polling untuk menu tombol inline.

Jalankan:  uv run --env-file .env bot/run_bot.py
"""
import os
import re
import sys
import json
import time
import threading

import telebot
from telebot import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "segrec"))
import db                                   # noqa: E402  (modul bersama, kontrak DB)
import arming                               # noqa: E402
import format as fmt                        # noqa: E402
import cut as segcut                        # noqa: E402  potong klip dari segmen (A/V)


TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])
DB_PATH = os.environ.get("CCTV_DB", "cctv.db")
OUT_DIR = os.environ.get("CCTV_OUT_DIR", "out/live")
ZONE_FILE = os.environ.get("CCTV_ZONE_FILE", "zones-102.json")
SEG_DIR = os.environ.get("SEG_DIR", "out/segments")
SEG_TIME = int(os.environ.get("SEG_TIME", "4"))
SEGCLIP_TMP = os.environ.get("SEGCLIP_TMP", "out/segclip-send.mp4")
POLL_INTERVAL = float(os.environ.get("BOT_POLL_S", "3"))
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# default setting bila belum ada di DB. clip_source: 'pipeline' (klip out/live) |
# 'segment' (potong dari segmen segrec -> beraudio & mulus; fallback ke pipeline).
DEFAULTS = {"notif_default": "aktif", "armed": "on",
            "send_close": "on", "send_loiter": "on", "send_transit": "on", "send_kucing": "on",
            "send_garasi": "on", "send_lewat": "off", "clip_source": "pipeline"}
CAMERAS_FILE = os.environ.get("CAMERAS_FILE", "cameras.json")   # sumber bersama (viewer + run_garasi)
# kedalaman default (mirror ZONE_DEPTH kode; utk editor zone-depth)
BUILTIN_DEPTH = {"jalan-masuk": 1, "tangga": 1, "taman": 2, "dekat-kolam": 2, "teras": 2, "pintu": 3}

bot = telebot.TeleBot(TOKEN, parse_mode=None)
_stop = threading.Event()
_pending_add = {}            # chat_id -> {"from":..,"to":..,"zone":..}
_attempts = {}              # event_id -> jumlah percobaan kirim


def milik_owner(chat_id):
    return str(chat_id) == CHAT_ID


def sget(con, key):
    return db.get_setting(con, key, DEFAULTS.get(key))


def zone_names():
    try:
        data = json.loads(open(ZONE_FILE).read())
        return sorted(data.get("zones", {}))
    except Exception:
        return sorted(BUILTIN_DEPTH)


# ── cameras.json (jadwal garasi; dibaca run_garasi live, diedit viewer + bot) ─────
def _read_cameras():
    try:
        return json.loads(open(CAMERAS_FILE).read())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "kamera": []}


def _garasi_cam(cfg):
    for k in cfg.get("kamera", []):
        if k.get("peran") == "garasi-ringan":
            return k
    return None


def _set_garasi(mut):
    """mut(cam) mengubah entri garasi in-place lalu simpan. Return cam / None."""
    cfg = _read_cameras()
    cam = _garasi_cam(cfg)
    if cam is None:
        return None
    mut(cam)
    with open(CAMERAS_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cam


def _win_label(jadwal):
    if not jadwal:
        return "24/7 (selalu aktif)"
    aktif = [f"{w['from']}–{w['to']}" for w in jadwal if w.get("aktif", True)]
    return ", ".join(aktif) if aktif else "(tak ada jendela aktif)"


WIN_PRESET = {"malam": ("🌙 Malam 22–04", [{"from": "22:00", "to": "04:00", "aktif": True}]),
              "kerja": ("🏢 Jam kerja 08–17", [{"from": "08:00", "to": "17:00", "aktif": True}]),
              "full": ("🔁 24/7 (selalu)", [])}


# ══ THREAD PENGIRIM ═════════════════════════════════════════════════════════════
def keputusan_kirim(con, ev, default, rules):
    """Status akhir untuk satu event: 'sent' (boleh) atau alasan skip."""
    if sget(con, "armed") != "on":
        return "skipped_disarmed"
    p = ev["payload"]
    if p.get("species") == "kucing" and sget(con, "send_kucing") != "on":
        return "skipped_filter"
    kat = {"close": "send_close", "loiter": "send_loiter", "lewat": "send_lewat",
           "garasi": "send_garasi"}.get(ev["kind"], "send_transit")
    if sget(con, kat) != "on":
        return "skipped_filter"
    if not arming.notif_aktif(default, rules, arming.tags_of(p), ev["ts"]):
        return "skipped_senyap"
    return "sent"


def kirim_media(con, ev):
    cap = fmt.caption(ev["payload"])
    path = None
    # garasi = alur terpisah tanpa arsip sendiri -> notif TEKS (jangan potong segmen taman)
    if ev["kind"] != "garasi" and sget(con, "clip_source") == "segment":   # potong dari segmen (A/V, mulus)
        t0, t1 = segcut.window_for(ev["payload"])
        try:
            path = segcut.cut(SEG_DIR, t0, t1, SEGCLIP_TMP, SEG_TIME)
        except Exception as e:
            print(f"[SEGCUT] gagal potong ({e!r}) -> fallback klip pipeline", flush=True)
    if path is None:                                        # fallback: klip pipeline out/live
        clip = ev.get("clip")
        p = os.path.join(OUT_DIR, clip) if clip else None
        path = p if (p and os.path.exists(p)) else None
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return bot.send_video(CHAT_ID, f, caption=cap, timeout=180)
    return bot.send_message(CHAT_ID, cap)      # klip tak ada -> tetap kabari via teks


def putaran_kirim():
    con = db.connect(DB_PATH)
    db.init_db(con)
    print("[BOT] pengirim mulai", flush=True)
    while not _stop.is_set():
        try:
            default = sget(con, "notif_default")
            rules = db.list_arming_rules(con, only_enabled=True)
            for ev in db.unsent_events(con, limit=20):
                status = keputusan_kirim(con, ev, default, rules)
                if status != "sent":
                    db.log_send(con, ev["id"], status)
                    print(f"[SKIP:{status}] event#{ev['id']} {ev['kind']}", flush=True)
                    continue
                try:
                    msg = kirim_media(con, ev)
                    db.log_send(con, ev["id"], "sent", getattr(msg, "message_id", None))
                    _attempts.pop(ev["id"], None)
                    print(f"[KIRIM] event#{ev['id']} {ev['kind']} -> {getattr(msg, 'message_id', '?')}", flush=True)
                except Exception as e:
                    n = _attempts.get(ev["id"], 0) + 1
                    _attempts[ev["id"]] = n
                    if n >= 3:
                        db.log_send(con, ev["id"], "failed")
                        _attempts.pop(ev["id"], None)
                        print(f"[ERROR] event#{ev['id']} gagal 3x, menyerah: {e!r}", flush=True)
                    else:
                        print(f"[ERROR] event#{ev['id']} gagal (coba {n}/3): {e!r}", flush=True)
        except Exception as e:
            print(f"[ERROR] putaran kirim: {e!r}", flush=True)
        _stop.wait(POLL_INTERVAL)
    con.close()
    print("[BOT] pengirim berhenti", flush=True)


# ══ LAYAR MENU (render text + keyboard per layar) ═══════════════════════════════
def _tombol(text, cb):
    return types.InlineKeyboardButton(text, callback_data=cb)


def layar_menu(con):
    default = sget(con, "notif_default")
    armed = sget(con, "armed")
    txt = (f"🛡️ CCTV Bot\n\n"
           f"Master: {'🟢 ARMED' if armed == 'on' else '⭕ DISARMED'}\n"
           f"Default notif: {'🔔 aktif' if default == 'aktif' else '🔕 senyap'}\n"
           f"Jendela jadwal: {len(db.list_arming_rules(con))}")
    kb = types.InlineKeyboardMarkup()
    kb.row(_tombol(f"🛡️ {'DISARM' if armed == 'on' else 'ARM'}", "armed:toggle"))
    kb.row(_tombol(("✅ " if default == "aktif" else "") + "🔔 Aktif", "def:aktif"),
           _tombol(("✅ " if default == "senyap" else "") + "🔕 Senyap", "def:senyap"))
    kb.row(_tombol("🔧 Filter jenis", "nav:filter"), _tombol("⚙️ Deteksi", "nav:deteksi"))
    kb.row(_tombol("🗓️ Jadwal", "nav:jadwal"), _tombol("📊 Ringkasan", "nav:ringkasan"))
    kb.row(_tombol("📷 Garasi", "nav:garasi"))
    return txt, kb


def layar_filter(con):
    txt = "🔧 Filter jenis notifikasi\n(centang = dikirim)"
    kb = types.InlineKeyboardMarkup()
    for key, label in [("send_close", "👤 Singgah (close)"), ("send_loiter", "⏳ Berlama (loiter)"),
                       ("send_transit", "🚪 Masuk/Keluar"), ("send_lewat", "👣 Lewat (sekejap)"),
                       ("send_kucing", "🐈 Kucing"), ("send_garasi", "🚗 Garasi")]:
        on = sget(con, key) == "on"
        kb.row(_tombol(("✅ " if on else "⬜ ") + label, f"flt:{key}"))
    kb.row(_tombol("⬅️ Menu", "nav:menu"))
    return txt, kb


def layar_deteksi(con):
    conf = db.get_setting(con, "det_conf")
    loiter = db.get_setting(con, "loiter_s")
    src = sget(con, "clip_source")
    src_lbl = "🎞️ SEGMEN (A/V, mulus)" if src == "segment" else "📼 PIPELINE (out/live)"
    txt = ("⚙️ Config deteksi (live, tanpa restart)\n\n"
           f"conf: {conf if conf else '(default CLI)'}\n"
           f"loiter_s: {loiter if loiter else '(default CLI)'}\n"
           f"Sumber klip: {src_lbl}\n\n"
           "conf tinggi = sedikit false-positive tapi bisa lewatkan orang samar.")
    kb = types.InlineKeyboardMarkup()
    kb.row(_tombol("🎚️ Set conf", "det:conf"), _tombol("⏱️ Set loiter_s", "det:loiter"))
    kb.row(_tombol("🧭 Zone-depth (masuk/keluar)", "nav:zdepth"))
    kb.row(_tombol(f"🔁 Sumber klip: {'->pipeline' if src == 'segment' else '->segmen'}", "clipsrc:toggle"))
    kb.row(_tombol("⬅️ Menu", "nav:menu"))
    return txt, kb


def layar_zdepth(con):
    ov = db.get_zone_depth(con)
    txt = ("🧭 Zone-depth — sumbu luar→dalam (0..3)\n"
           "1=tepi properti · 2=halaman · 3=ambang rumah\n"
           "Tekan zona untuk memutar nilainya. ⚠️ memengaruhi deteksi MASUK/KELUAR.")
    kb = types.InlineKeyboardMarkup()
    for z in zone_names():
        d = ov.get(z, BUILTIN_DEPTH.get(z, 0))
        tanda = "•" if z in ov else " "
        kb.row(_tombol(f"{tanda} {z}: {d}", f"zd:{z}"))
    kb.row(_tombol("⬅️ Deteksi", "nav:deteksi"))
    return txt, kb


def layar_jadwal(con):
    txt = "🗓️ Jendela senyap/aktif (klik untuk hapus):"
    kb = types.InlineKeyboardMarkup()
    for r in db.list_arming_rules(con):
        z = ",".join(r["zones"]) if r["zones"] else "semua"
        stat = "" if r["enabled"] else " (off)"
        kb.row(_tombol(f"🗑️ {r['from']}–{r['to']} · {r['notif']} · {z}{stat}", f"del:{r['id']}"))
    kb.row(_tombol("➕ Tambah jendela", "add"))
    kb.row(_tombol("⬅️ Menu", "nav:menu"))
    return txt, kb


def layar_garasi(con):
    cfg = _read_cameras()
    cam = _garasi_cam(cfg)
    if cam is None:
        kb = types.InlineKeyboardMarkup()
        kb.row(_tombol("⬅️ Menu", "nav:menu"))
        return "📷 Garasi\n\nKamera peran 'garasi-ringan' belum ada di cameras.json.", kb
    en = cam.get("enabled", True)
    gsend = sget(con, "send_garasi") == "on"
    txt = ("📷 Deteksi garasi (ringan)\n\n"
           f"Deteksi: {'🟢 ON' if en else '⭕ OFF'}\n"
           f"Notif garasi: {'🔔 aktif' if gsend else '🔕 senyap'}\n"
           f"Jendela: {_win_label(cam.get('jadwal', []))}\n\n"
           "Di luar jendela: proses hidup, tak inferensi/notif (hemat GPU). Berlaku live.")
    kb = types.InlineKeyboardMarkup()
    kb.row(_tombol(f"🛡️ {'Matikan' if en else 'Nyalakan'} deteksi", "gar:toggle"))
    kb.row(_tombol(("🔕 Bungkam notif" if gsend else "🔔 Aktifkan notif"), "garsend"))
    for key, (label, _) in WIN_PRESET.items():
        kb.row(_tombol(label, f"garwin:{key}"))
    kb.row(_tombol("⬅️ Menu", "nav:menu"))
    return txt, kb


PERIODE = {"hari": ("Hari ini", None), "24j": ("24 jam", 24 * 3600), "7h": ("7 hari", 7 * 24 * 3600)}


def _since(key):
    if key == "hari":
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return time.time() - PERIODE[key][1]


def layar_ringkasan(con, key="24j"):
    since = _since(key)
    total = db.count_since(con, since)
    rows = db.summary_since(con, since)
    bh = db.busiest_hour_since(con, since)
    baris = [f"📊 Ringkasan · {PERIODE[key][0]}", f"Total event: {total}"]
    if bh:
        baris.append(f"Jam tersibuk: {bh[0]}:00 ({bh[1]})")
    perkind = {}
    perzona = {}
    for r in rows:
        perkind[r["kind"]] = perkind.get(r["kind"], 0) + r["n"]
        if r["zone"]:
            perzona[r["zone"]] = perzona.get(r["zone"], 0) + r["n"]
    if perkind:
        baris.append("\nPer jenis:")
        for k, n in sorted(perkind.items(), key=lambda x: -x[1]):
            baris.append(f"• {k}: {n}")
    if perzona:
        baris.append("\nZona teratas:")
        for z, n in sorted(perzona.items(), key=lambda x: -x[1])[:5]:
            baris.append(f"• {z}: {n}")
    kb = types.InlineKeyboardMarkup()
    kb.row(*[_tombol(("✅ " if k == key else "") + v[0], f"ring:{k}") for k, v in PERIODE.items()])
    kb.row(_tombol("⬅️ Menu", "nav:menu"))
    return "\n".join(baris), kb


LAYAR = {"menu": layar_menu, "filter": layar_filter, "deteksi": layar_deteksi,
         "zdepth": layar_zdepth, "jadwal": layar_jadwal, "garasi": layar_garasi}


def tampil(con, chat_id, message_id, nama, **kw):
    txt, kb = (layar_ringkasan(con, **kw) if nama == "ringkasan" else LAYAR[nama](con))
    try:
        bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, txt, reply_markup=kb)


# ══ HANDLER ═════════════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start", "menu"])
def cmd_menu(message):
    if not milik_owner(message.chat.id):
        return
    con = db.connect(DB_PATH)
    db.init_db(con)
    txt, kb = layar_menu(con)
    bot.send_message(message.chat.id, txt, reply_markup=kb)
    con.close()


@bot.message_handler(commands=["ringkasan"])
def cmd_ringkasan(message):
    if not milik_owner(message.chat.id):
        return
    con = db.connect(DB_PATH)
    db.init_db(con)
    txt, kb = layar_ringkasan(con, "24j")
    bot.send_message(message.chat.id, txt, reply_markup=kb)
    con.close()


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    if not milik_owner(c.message.chat.id):
        return
    con = db.connect(DB_PATH)
    db.init_db(con)
    chat, mid, data = c.message.chat.id, c.message.message_id, c.data
    try:
        if data.startswith("nav:"):
            tampil(con, chat, mid, data.split(":", 1)[1]); bot.answer_callback_query(c.id)
        elif data == "armed:toggle":
            db.set_setting(con, "armed", "off" if sget(con, "armed") == "on" else "on")
            tampil(con, chat, mid, "menu"); bot.answer_callback_query(c.id, "OK")
        elif data.startswith("def:"):
            db.set_setting(con, "notif_default", data.split(":", 1)[1])
            tampil(con, chat, mid, "menu"); bot.answer_callback_query(c.id, "Tersimpan")
        elif data.startswith("flt:"):
            k = data.split(":", 1)[1]
            db.set_setting(con, k, "off" if sget(con, k) == "on" else "on")
            tampil(con, chat, mid, "filter"); bot.answer_callback_query(c.id)
        elif data == "garsend":
            db.set_setting(con, "send_garasi", "off" if sget(con, "send_garasi") == "on" else "on")
            tampil(con, chat, mid, "garasi"); bot.answer_callback_query(c.id, "OK")
        elif data == "gar:toggle":
            ok = _set_garasi(lambda k: k.update(enabled=not k.get("enabled", True)))
            tampil(con, chat, mid, "garasi"); bot.answer_callback_query(c.id, "OK" if ok else "garasi tak ada")
        elif data.startswith("garwin:"):
            preset = WIN_PRESET.get(data.split(":", 1)[1])
            if preset is not None:
                _set_garasi(lambda k: k.update(jadwal=[dict(w) for w in preset[1]]))
            tampil(con, chat, mid, "garasi"); bot.answer_callback_query(c.id, "OK")
        elif data.startswith("ring:"):
            tampil(con, chat, mid, "ringkasan", key=data.split(":", 1)[1]); bot.answer_callback_query(c.id)
        elif data.startswith("zd:"):
            z = data.split(":", 1)[1]
            cur = db.get_zone_depth(con).get(z, BUILTIN_DEPTH.get(z, 0))
            db.set_zone_depth(con, z, (cur + 1) % 4)
            tampil(con, chat, mid, "zdepth"); bot.answer_callback_query(c.id, f"{z} -> {(cur + 1) % 4}")
        elif data == "clipsrc:toggle":
            db.set_setting(con, "clip_source",
                           "pipeline" if sget(con, "clip_source") == "segment" else "segment")
            tampil(con, chat, mid, "deteksi"); bot.answer_callback_query(c.id, "OK")
        elif data == "det:conf":
            bot.send_message(chat, "Kirim nilai conf (0–1, mis. 0.15):")
            bot.register_next_step_handler_by_chat_id(chat, langkah_conf); bot.answer_callback_query(c.id)
        elif data == "det:loiter":
            bot.send_message(chat, "Kirim loiter_s (detik, mis. 30):")
            bot.register_next_step_handler_by_chat_id(chat, langkah_loiter); bot.answer_callback_query(c.id)
        elif data.startswith("del:"):
            db.delete_arming_rule(con, int(data.split(":", 1)[1]))
            tampil(con, chat, mid, "jadwal"); bot.answer_callback_query(c.id, "Dihapus")
        elif data == "add":
            _pending_add[chat] = {}
            bot.send_message(chat, "Kirim jam MULAI (HH:MM), mis. 22:00")
            bot.register_next_step_handler_by_chat_id(chat, langkah_mulai); bot.answer_callback_query(c.id)
        elif data.startswith("addzone:"):
            z = data.split(":", 1)[1]
            _pending_add.setdefault(chat, {})["zone"] = None if z == "semua" else z
            kb = types.InlineKeyboardMarkup()
            kb.row(_tombol("🔕 Senyap", "addnotif:senyap"), _tombol("🔔 Aktif", "addnotif:aktif"))
            bot.edit_message_text("Setel jendela jadi:", chat, mid, reply_markup=kb)
            bot.answer_callback_query(c.id)
        elif data.startswith("addnotif:"):
            st = _pending_add.get(chat)
            if not st or "from" not in st or "to" not in st:
                bot.answer_callback_query(c.id, "Alur kadaluarsa")
            else:
                zones = [] if st.get("zone") is None else [st["zone"]]
                db.add_arming_rule(con, zones, st["from"], st["to"], data.split(":", 1)[1])
                _pending_add.pop(chat, None)
                txt, kb = layar_jadwal(con)
                bot.edit_message_text("✅ Ditambah.\n\n" + txt, chat, mid, reply_markup=kb)
                bot.answer_callback_query(c.id, "Tersimpan")
    finally:
        con.close()


# ── alur multi-langkah (input teks) ─────────────────────────────────────────────
def _minta_ulang(chat, pesan, handler):
    bot.send_message(chat, pesan)
    bot.register_next_step_handler_by_chat_id(chat, handler)


def langkah_mulai(message):
    if not milik_owner(message.chat.id):
        return
    val = (message.text or "").strip()
    if not HHMM_RE.match(val):
        return _minta_ulang(message.chat.id, "Format salah. Jam MULAI HH:MM (mis. 22:00)", langkah_mulai)
    _pending_add.setdefault(message.chat.id, {})["from"] = val
    _minta_ulang(message.chat.id, "Kirim jam SELESAI (HH:MM), mis. 06:00", langkah_selesai)


def langkah_selesai(message):
    if not milik_owner(message.chat.id):
        return
    val = (message.text or "").strip()
    if not HHMM_RE.match(val):
        return _minta_ulang(message.chat.id, "Format salah. Jam SELESAI HH:MM (mis. 06:00)", langkah_selesai)
    _pending_add.setdefault(message.chat.id, {})["to"] = val
    kb = types.InlineKeyboardMarkup()
    kb.row(_tombol("🌐 Semua zona", "addzone:semua"))
    for z in zone_names():
        kb.row(_tombol(f"📍 {z}", f"addzone:{z}"))
    bot.send_message(message.chat.id, "Berlaku untuk zona mana?", reply_markup=kb)


def langkah_conf(message):
    if not milik_owner(message.chat.id):
        return
    try:
        v = float((message.text or "").strip().replace(",", "."))
        assert 0 < v <= 1
    except Exception:
        return _minta_ulang(message.chat.id, "Nilai tak valid. conf 0–1 (mis. 0.15):", langkah_conf)
    con = db.connect(DB_PATH); db.init_db(con)
    db.set_setting(con, "det_conf", str(v)); con.close()
    bot.send_message(message.chat.id, f"✅ conf -> {v} (berlaku live). /menu untuk kembali.")


def langkah_loiter(message):
    if not milik_owner(message.chat.id):
        return
    try:
        v = float((message.text or "").strip().replace(",", "."))
        assert v > 0
    except Exception:
        return _minta_ulang(message.chat.id, "Nilai tak valid. loiter_s detik (mis. 30):", langkah_loiter)
    con = db.connect(DB_PATH); db.init_db(con)
    db.set_setting(con, "loiter_s", str(v)); con.close()
    bot.send_message(message.chat.id, f"✅ loiter_s -> {v} (berlaku live). /menu untuk kembali.")


def main():
    con = db.connect(DB_PATH)
    db.init_db(con)
    con.close()
    t = threading.Thread(target=putaran_kirim, name="pengirim", daemon=True)
    t.start()
    print("[BOT] telebot polling mulai", flush=True)
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    finally:
        _stop.set()
        t.join(timeout=5)


if __name__ == "__main__":
    main()
