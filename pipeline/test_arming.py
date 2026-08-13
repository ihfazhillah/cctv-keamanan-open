"""Test jadwal arming (mode A: senyap = tetap rekam, cuma tak kirim Telegram).

    uv run pipeline/test_arming.py

Nol-deps. Logika murni notif_aktif(schedule, tags, epoch) + in_window.
"""

import datetime
from live import notif_aktif, in_window


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}  got: {got}")
    return got == want


def ep(h, m=0):
    return datetime.datetime(2026, 8, 12, h, m).timestamp()   # waktu LOKAL


def main():
    r = []

    # malam teras senyap 22:00-06:00 (lewat tengah malam)
    night = {"default": "aktif", "rules": [
        {"zones": ["teras", "pintu"], "from": "22:00", "to": "06:00", "notif": "senyap"}]}
    r.append(check("A1 malam teras -> senyap", notif_aktif(night, {"teras"}, ep(23)), False))
    r.append(check("A2 siang teras -> aktif (default)", notif_aktif(night, {"teras"}, ep(12)), True))
    r.append(check("A3 malam taman (zona tak cocok) -> aktif", notif_aktif(night, {"taman"}, ep(23)), True))
    r.append(check("A4 03:00 teras masih dalam wrap -> senyap", notif_aktif(night, {"teras"}, ep(3)), False))

    # aturan tanpa zones berlaku utk semua
    allz = {"default": "aktif", "rules": [{"from": "00:00", "to": "07:00", "notif": "senyap"}]}
    r.append(check("A5 03:00 apa saja -> senyap", notif_aktif(allz, {"apapun"}, ep(3)), False))
    r.append(check("A6 09:00 apa saja -> aktif", notif_aktif(allz, {"apapun"}, ep(9)), True))

    # cocok TERAKHIR menang
    last = {"default": "aktif", "rules": [
        {"from": "00:00", "to": "24:00", "notif": "senyap"},
        {"zones": ["teras"], "from": "06:00", "to": "22:00", "notif": "aktif"}]}
    r.append(check("A7 teras siang: aturan-2 aktif menang", notif_aktif(last, {"teras"}, ep(12)), True))
    r.append(check("A8 taman siang: cuma aturan-1 -> senyap", notif_aktif(last, {"taman"}, ep(12)), False))

    # transit: tags = gerbang
    r.append(check("A9 transit gerbang rumah malam -> senyap",
                   notif_aktif(night, {"rumah", "property"}, ep(23)) if False else
                   notif_aktif({"default": "aktif", "rules": [
                       {"zones": ["rumah"], "from": "22:00", "to": "06:00", "notif": "senyap"}]},
                       {"rumah", "property"}, ep(23)), False))

    # default senyap
    r.append(check("A10 default senyap", notif_aktif({"default": "senyap", "rules": []}, {"x"}, ep(12)), False))

    # in_window
    r.append(check("A11 in_window siang", in_window("06:00", "22:00", 12 * 60), True))
    r.append(check("A12 in_window wrap 23:00", in_window("22:00", "06:00", 23 * 60), True))
    r.append(check("A13 in_window wrap siang -> luar", in_window("22:00", "06:00", 12 * 60), False))

    print()
    if all(r):
        print(f"ALL PASS ({len(r)}/{len(r)})")
    else:
        print(f"FAIL {r.count(False)}/{len(r)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
