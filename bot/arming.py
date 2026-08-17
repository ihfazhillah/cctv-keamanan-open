"""Evaluasi arming (murni) — diangkat dari pipeline/live.py agar service bot
berdiri sendiri (tak menyeret dependensi pipeline). 'senyap' = event tetap
tersimpan di DB, cuma tak dikirim ke Telegram (mode A)."""
import time


def _hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(frm, to, minutes):
    """menit-hari `minutes` ada di [frm,to)? Dukung lewat tengah malam (frm>to)."""
    a, b = _hhmm(frm), _hhmm(to)
    if a <= b:
        return a <= minutes < b
    return minutes >= a or minutes < b


def notif_aktif(default, rules, tags, epoch):
    """True bila notifikasi harus DIKIRIM untuk event ber-`tags` pada `epoch`.
    rules = list dict {zones:[], from, to, notif}. Yang COCOK TERAKHIR menang;
    tanpa yang cocok -> default. `tags` = himpunan zona/gerbang event."""
    lt = time.localtime(epoch)
    minutes = lt.tm_hour * 60 + lt.tm_min
    hasil = default
    tags = set(tags)
    for rule in rules:
        rz = rule.get("zones")
        if rz and not (set(rz) & tags):
            continue
        if not in_window(rule.get("from", "00:00"), rule.get("to", "24:00"), minutes):
            continue
        hasil = rule.get("notif", "aktif")
    return hasil == "aktif"


def tags_of(payload):
    """Zona/gerbang event (untuk pencocokan rule per-zona)."""
    if payload.get("zone"):
        return {payload["zone"]}          # close / loiter
    return set(payload.get("gates", []))  # transit keluar/masuk
