"""Pembentukan caption Telegram — presentasi milik BOT (inti tak tahu format).
Mirror dari pipeline/run_live.py (loiter/close/transit) tapi baca `payload` DB."""
import datetime


ZONE_EMOJI = {
    "teras": "🚪", "pintu": "🚪",
    "taman": "🌳",
    "dekat-kolam": "💧",
    "jalan-masuk": "🪜", "tangga": "🪜",
    "jalan-utama": "🛣️",
}


def zemoji(zone):
    return ZONE_EMOJI.get(zone, "📍")


def jam(t):
    return f"{datetime.datetime.fromtimestamp(t):%H:%M:%S}"


def _loiter(ev):
    loiter_s = ev["at"] - ev["start"]
    z = ev["zone"]
    aksi = "KUCING MANGKAL" if ev.get("species") == "kucing" else "ORANG BERLAMA"
    return f"⏳ {aksi} · {zemoji(z)} {z}\nsudah {loiter_s:.0f} detik · sejak {jam(ev['start'])}"


def _close(ev):
    dwell_s = ev["end"] - ev["start"]
    satuan = "detik"
    if dwell_s > 60:
        dwell_s = dwell_s / 60
        satuan = "menit"
    z = ev["zone"]
    lead, subj = ("🐈", "KUCING") if ev.get("species") == "kucing" else ("👤", "ORANG")
    return f"{lead} {subj} · {zemoji(z)} {z}\n{dwell_s:.1f} {satuan} · {jam(ev['start'])}–{jam(ev['end'])}"


def _transit(ev):
    gates = ev.get("gates", [])
    masuk = ev["kind"] == "masuk"
    lead = "🟢" if masuk else "🔴"
    label = "MASUK" if masuk else "KELUAR"
    utama = "RUMAH" if "rumah" in gates else "PROPERTY"
    via = "property" if (utama == "RUMAH" and "property" in gates) else None
    baris2 = f"via {via} · {jam(ev['start'])}" if via else jam(ev["start"])
    return f"{lead} {label} {utama}\n{baris2}"


def _episode(ev):
    arah = ev.get("arah")
    gates = ev.get("gates", [])
    masuk = arah == "masuk"
    lead = "🟢" if masuk else "🔴"
    label = "MASUK" if masuk else "KELUAR"
    utama = "RUMAH" if "pintu" in gates else "PROPERTY"     # ambang pintu tersentuh = level rumah
    dur = ev.get("end", ev.get("start", 0)) - ev.get("start", 0)
    jalur = " → ".join(gates[:5]) if gates else ""
    return f"{lead} {label} {utama}\n{jam(ev.get('start', 0))} · {dur:.0f}s · {jalur}"


def _garasi(ev):
    if ev.get("lintas") and ev.get("arah"):
        garis = ev.get("garis")
        inti = f"{ev['arah']} garis {garis}" if garis else f"lintas {ev['arah']}"
        return f"🚶 GARASI — {inti}\n{jam(ev.get('at', 0))}"
    return f"🚗 ORANG DI GARASI\n{jam(ev.get('at', 0))}"


def caption(payload):
    match payload.get("kind"):
        case "loiter": return _loiter(payload)
        case "close": return _close(payload)
        case "episode": return _episode(payload)
        case "garasi": return _garasi(payload)
        case _: return _transit(payload)     # keluar / masuk
