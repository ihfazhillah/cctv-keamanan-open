"""Test consumer + shutdown sentinel — Piece #2 skeleton producer/consumer.

    uv run pipeline/test_consumer.py      (atau: python3 pipeline/test_consumer.py)

Tanpa dependency (stdlib: queue, threading) → jalan di mesin mana pun, tak butuh GPU/RTSP.

KONTRAK yang diuji:
    SENTINEL                     -> penanda "selesai", identitas UNIK (object()), BUKAN None.
    consume(q, handle)           -> loop: ev = q.get(); kalau ev is SENTINEL -> berhenti;
                                    selain itu handle(ev). Kerja lambat ada di handle,
                                    consumer-nya sendiri agnostik isi event.

Pola producer (pasangannya): put semua event, lalu put SENTINEL.
    for ev in events: q.put(ev)
    q.put(SENTINEL)              # marker lewat queue yang SAMA (FIFO) -> tiba SETELAH
                                 # semua event nyata -> graceful drain (tak ada yg jatuh).

Kenapa SENTINEL = object() dan bukan None: sentinel harus nilai yang TAK MUNGKIN jadi
event sah. None/0/"" bisa jadi payload sah -> pakai objek unik + bandingkan dengan `is`
(identitas), bukan `==`/truthiness. Test IDENTITAS di bawah SENGAJA mengirim None/0/""
sebagai event sah; kalau kamu set SENTINEL=None atau pakai `if not ev`, test itu GAGAL.

CATATAN HARNESS: tiap consume dijalankan di DAEMON THREAD + join(timeout). Kalau
implementasimu tak pernah berhenti (mis. lupa tangani SENTINEL), thread menggantung ->
test melaporkan FAIL "consumer tak berhenti", BUKAN ikut menggantung selamanya.
(daemon=True supaya proses test tetap bisa keluar meski ada thread nyangkut.)

Fake payload = string ("e0","e1",...) supaya hasil mudah dibaca & di-assert.
"""

import queue
import threading

from live import consume, SENTINEL

TIMEOUT = 2.0  # detik; consumer yang benar berhenti < milidetik, ini kelonggaran besar.


def check(name, got, want):
    status = "ok  " if got == want else "FAIL"
    print(f"  [{status}] {name}")
    if got != want:
        print(f"         want: {want}")
        print(f"         got : {got}")
    return got == want


def run_prefilled(items):
    """Isi queue dg `items` (urut) DULU, baru jalankan consume di daemon thread.
    Kembalikan (seen, q, finished). finished=False -> consumer MENGGANTUNG (bug)."""
    q = queue.Queue()
    for it in items:
        q.put(it)
    seen = []
    t = threading.Thread(target=consume, args=(q, seen.append), daemon=True)
    t.start()
    t.join(TIMEOUT)
    return seen, q, (not t.is_alive())


def main():
    results = []

    # 1) DRAIN URUT LALU BERHENTI di sentinel; apa pun SETELAH sentinel tak disentuh.
    seen, q, fin = run_prefilled(["e0", "e1", "e2", SENTINEL, "e3-setelah-stop"])
    results.append(check("berhenti bersih (tak menggantung)", fin, True))
    results.append(check("proses urut lalu stop di sentinel", seen, ["e0", "e1", "e2"]))
    leftover = q.get_nowait() if not q.empty() else "<queue kosong>"
    results.append(check("item setelah sentinel TAK dikonsumsi", leftover, "e3-setelah-stop"))

    # 2) IDENTITAS, BUKAN NILAI — None / 0 / "" adalah event SAH, harus diproses,
    #    bukan disalahartikan sebagai sinyal stop. (Gagal bila SENTINEL=None / `if not ev`.)
    seen, _, fin = run_prefilled([None, 0, "", "e-nyata", SENTINEL])
    results.append(check("None/0/'' event sah: berhenti bersih", fin, True))
    results.append(check("berhenti hanya pada IDENTITAS sentinel (bukan falsy/None)",
                         seen, [None, 0, "", "e-nyata"]))

    # 3) GRACEFUL DRAIN — semua event yang mengantre sebelum sentinel terproses, nol jatuh.
    banyak = [f"e{i}" for i in range(50)]
    seen, _, fin = run_prefilled(banyak + [SENTINEL])
    results.append(check("graceful: 50 event antre habis sebelum tutup", seen, banyak))
    results.append(check("graceful: berhenti bersih setelah kuras", fin, True))

    # 4) SENTINEL SAJA (tak ada kerja) -> langsung berhenti, seen kosong.
    seen, _, fin = run_prefilled([SENTINEL])
    results.append(check("sentinel saja -> berhenti, tak ada kerja", (fin, seen), (True, [])))

    # 5) SHUTDOWN NYATA saat consumer sedang TIDUR di get() (queue mulai KOSONG),
    #    lalu producer kirim event + sentinel BELAKANGAN. Menguji jalur blokir->bangun->tutup
    #    (yang tak tersentuh test 1-4 karena queue sudah terisi duluan).
    q = queue.Queue()
    seen = []
    t = threading.Thread(target=consume, args=(q, seen.append), daemon=True)
    t.start()                 # consumer langsung memblok di get() (queue kosong)
    q.put("e-live")           # bangunkan & proses satu event
    q.put(SENTINEL)           # lalu beri tahu "selesai"
    t.join(TIMEOUT)
    results.append(check("blokir->bangun->tutup: thread keluar bersih", t.is_alive(), False))
    results.append(check("blokir->bangun->tutup: sempat proses event", seen, ["e-live"]))

    print()
    if all(results):
        print(f"ALL PASS ({len(results)}/{len(results)})")
    else:
        print(f"FAIL {results.count(False)}/{len(results)} — perbaiki lalu ulang.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
