"""Test PendingNotifier — SIMPAN & LENGKAPI frame tiap trigger, digerakkan kedatangan frame.

    uv run pipeline/test_pending.py   (atau: python3 ...)

Nol-deps (frame = string palsu). Logika MURNI, tak butuh cv2.

KENAPA menyimpan frame sendiri (bukan cuma window):
    Versi awal cuma menyimpan [t0,t1] lalu `buffer.get` di saat due -> masih bergantung buffer
    bersama yang bisa MENGEVIKSI frame sebelum due -> "data sudah hilang". Fix (user turunkan):
    tiap pending MENYIMPAN frame-nya sendiri, BERTAMBAH sampai lengkap. Maka `frame_buffer`
    menciut jadi pelacak PRE saja (tetap BODOH), sementara pending "menunggu sambil melengkapi
    frame tiap detak". keep_s buffer bisa kecil (~PRE), bukan PRE+POST+durasi-episode.

KONTRAK:
    class PendingNotifier:
        def __init__(self, pre, post)
        def add(self, trigger, pre_frames)   # buka pending; window per-kind; frames = SALINAN pre_frames
        def feed(self, t, frame)             # tiap frame: append ke pending TERBUKA yg t0 <= t <= t1
        def due(self, t) -> list             # pending lengkap (t1 <= t) BESERTA frames; keluarkan

    WINDOW per kind: close [start-pre, end+post] | loiter [at-pre, at+post] | passage [at-pre, at].
    - `feed` mengabaikan frame di luar window (t<t0 atau t>t1) & pending yang belum/tak lagi terbuka.
    - passage: t1=at (lampau) -> lahir-lengkap, frame-nya cuma dari pre_frames (tak diakumulasi).
    - `add` MENYALIN pre_frames (list(...)) supaya list pemanggil tak ter-alias.
    - due mengeluarkan yang lengkap (tak muncul dua kali -> notif dobel).

    Pending yang dikembalikan = {"t0","t1","trigger","frames"}. Notifier baca trigger utk caption/
    format & pakai frames apa adanya (PendingNotifier soal WAKTU+AKUMULASI; format+kirim = notifier).
"""

from live import PendingNotifier


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def main():
    r = []

    # -- WINDOW & WAKTU --
    # W1) close window; trigger asal tersimpan.
    pn = PendingNotifier(pre=5, post=5)
    pn.add({"kind": "close", "zone": "teras", "start": 100.0, "end": 105.0}, [])
    out = pn.due(110.0)
    r.append(check("W1 close t0/t1 = [start-pre, end+post]", (out[0]["t0"], out[0]["t1"]), (95.0, 110.0)))
    r.append(check("W1 trigger asal tersimpan", out[0]["trigger"]["zone"], "teras"))

    # W2) loiter = SELURUH masa berdiam [start, at+post] (bukan cuma sekitar ambang).
    #     at = start + loiter_s; klip mencakup dari kedatangan sampai post. Butuh
    #     buffer >= loiter_s (ClipBuffer.keep_s diskalakan ke --loiter_s).
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "loiter", "zone": "z", "start": 100.0, "at": 130.0}, [])
    out = pn.due(135.0)
    r.append(check("W2 loiter t0/t1 = [start, at+post]", (out[0]["t0"], out[0]["t1"]), (100.0, 135.0)))

    # W3) passage t1 = at -> lahir-lengkap -> due seketika.
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "MASUK rumah", "at": 100.0}, [])
    out = pn.due(100.0)
    r.append(check("W3 passage due @at, t1=at", (len(out), out[0]["t1"]), (1, 100.0)))

    # D1..D3) waktu & PENGHAPUSAN.
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "close", "zone": "z", "start": 100.0, "end": 105.0}, [])   # t1=110
    r.append(check("D1 belum due @109 -> []", pn.due(109.0), []))
    r.append(check("D2 due @110 (batas t1<=t)", len(pn.due(110.0)), 1))
    r.append(check("D3 tak muncul lagi @111", pn.due(111.0), []))

    # M1) selektif: hanya yang due keluar; sisanya bertahan.
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "close", "zone": "a", "start": 100.0, "end": 105.0}, [])   # t1=110
    pn.add({"kind": "close", "zone": "b", "start": 200.0, "end": 205.0}, [])   # t1=210
    r.append(check("M1 hanya 'a' @110", [p["trigger"]["zone"] for p in pn.due(110.0)], ["a"]))
    r.append(check("M1 'b' keluar @210", [p["trigger"]["zone"] for p in pn.due(210.0)], ["b"]))

    # -- FRAME: simpan & lengkapi --
    # F1) close: frames = pre_frames + frame in-window; abaikan early(<t0) & late(>t1).
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "close", "zone": "a", "start": 100.0, "end": 105.0}, ["p1", "p2"])  # [95,110]
    pn.feed(90.0, "early")    # < t0 -> abaikan
    pn.feed(106.0, "mid")     # dalam window -> append
    pn.feed(115.0, "late")    # > t1 -> abaikan
    out = pn.due(110.0)
    r.append(check("F1 frames = pre + in-window (abaikan luar)", out[0]["frames"], ["p1", "p2", "mid"]))

    # F2) passage lahir-lengkap: frames = pre_frames saja, tak diakumulasi sesudahnya.
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "KELUAR rumah", "at": 100.0}, ["snap"])   # [95,100]
    pn.feed(101.0, "sesudah")   # > t1(100) -> abaikan
    out = pn.due(100.0)
    r.append(check("F2 passage frames = pre saja", out[0]["frames"], ["snap"]))

    # F3) feed selektif: frame masuk hanya ke pending yang windownya mencakupnya.
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "close", "zone": "a", "start": 100.0, "end": 105.0}, [])  # [95,110]
    pn.add({"kind": "close", "zone": "b", "start": 200.0, "end": 205.0}, [])  # [195,210]
    pn.feed(105.0, "x")   # hanya di window 'a'
    r.append(check("F3 'a' dapat x", pn.due(110.0)[0]["frames"], ["x"]))
    r.append(check("F3 'b' kosong", pn.due(210.0)[0]["frames"], []))

    # F4) add MENYALIN pre_frames: mutasi list pemanggil tak merembes ke pending.
    pn = PendingNotifier(5, 5)
    seed = ["s1"]
    pn.add({"kind": "close", "zone": "a", "start": 100.0, "end": 105.0}, seed)
    seed.append("s2")     # mutasi luar
    r.append(check("F4 pending tak ter-alias mutasi luar", pn.due(110.0)[0]["frames"], ["s1"]))

    # F5) batas: feed persis di t1 IKUT (paku <=).
    pn = PendingNotifier(5, 5)
    pn.add({"kind": "close", "zone": "a", "start": 100.0, "end": 105.0}, [])  # t1=110
    pn.feed(110.0, "edge")
    r.append(check("F5 frame di t1 ikut (<=)", pn.due(110.0)[0]["frames"], ["edge"]))

    print()
    if all(r):
        print(f"ALL PASS ({len(r)}/{len(r)})")
    else:
        print(f"FAIL {r.count(False)}/{len(r)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
