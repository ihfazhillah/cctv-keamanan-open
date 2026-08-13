# episode from events.jsonl -> table SQLite. Untuk menjawab "kapan ada orang di kolam"

import argparse
import sqlite3
import json
import datetime
from pathlib import Path

EVENTS = Path("out/events.jsonl")
DB = Path("out/events.db")


GATES = [
    {"zones": {"teras"}, "lahir": "KELUAR rumah", "mati": "MASUK rumah"},
    {"zones": {"jalan-masuk", "tangga"}, "lahir" : "MASUK property", "mati": "KELUAR property"}
]

def detect_passages(property_eps, ambang, gates=GATES):
    """
    property_eps: list (dt_start, dt_end, zone, start_s, end_s)

    Returns: list passge {kind, wall}
    """
    presences = []
    for start, end, zone, start_s, end_s in sorted(property_eps, key=lambda e: e[0]):
        if presences and presences[-1]["end"] + ambang >= start:
            cur = presences[-1]
            if end > cur["end"]:
                cur["end"] = end
                cur["end_s"] = end_s
                cur["last_zone"] = zone
        else:
            presences.append({"start": start, "end": end, "first_zone": zone, "last_zone": zone, "start_s": start_s, "end_s": end_s})

    passages = []
    for p in presences:
        for gate in gates:
            if p["first_zone"] in gate["zones"]:
                passages.append({"kind": gate["lahir"], "wall": p["start"], "video_s": p["start_s"]})
            if p["last_zone"] in gate["zones"]:
                passages.append({"kind": gate["mati"], "wall": p["end"], "video_s": p["end_s"]})

    return passages

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zone", type=str, default=None)
    parser.add_argument("--start-time", type=str, default=None)
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    cur = con.cursor()

    # buat table
    # schema: zone TEXT, start_s REAL, end_s REAL, track_ids TEXT untuk simpan json list, start_wall TEXT 
    cur.execute("""
        CREATE TABLE IF NOT EXISTS episodes ( 
            zone TEXT,
            start_s REAL,
            end_s REAL,
            track_ids TEXT,
            start_wall TEXT
        )
    """)

    # make it idempotent. For now, because the jsonl is our source of truth, so better to
    # remove all rows
    cur.execute("DELETE FROM episodes")

    # ingest

    if args.start_time:
        start_time = datetime.datetime.fromisoformat(args.start_time)
    else:
        start_time = datetime.datetime.now()

    with EVENTS.open() as f:
        for line in f:
            ev = json.loads(line)

            if ev.get("type") != "episode_tracker":
                continue

            start = start_time + datetime.timedelta(seconds=ev["start_s"])

            cur.execute(
                "INSERT INTO episodes (zone, start_s, end_s, track_ids, start_wall) VALUES (?, ?, ?, ?, ?)",
                (ev["zone"], ev["start_s"], ev["end_s"], json.dumps(ev["track_ids"]), start.isoformat(sep=" ", timespec="seconds"))
            )

    con.commit()

    if args.zone:
        print(f"Episode di zona {args.zone}: ")
        for row in cur.execute(
            "SELECT start_s, end_s, start_wall from episodes WHERE zone = ? ORDER BY start_s", (args.zone,)
        ):
            print(f"\tterisi: {row[0]:.1f}s -> {row[1]:.1f}s")

    else:
        abaikan = {"jalan-utama"} # ingat ini set, bukan list
        property_eps = [] # daftar (dt_start, dt_end, zone)

        ambang = datetime.timedelta(seconds=3) # todo: nanti bisa diset

        events = []
        presences = []
        print("All episodes: ")
        for row in cur.execute(
            "SELECT zone, start_s, end_s, start_wall from episodes" 
        ):
            dt_start = datetime.datetime.fromisoformat(row[3])
            dt_end = dt_start + datetime.timedelta(seconds=row[2] - row[1])
            if row[0] not in abaikan:
                property_eps.append((dt_start, dt_end, row[0], row[1], row[2]))

            events.append((dt_start, row[0], "TERISI"))
            events.append((dt_end, row[0], "KOSONG"))

            # print(f"{row[0]} terisi: {row[1]:.1f}s -> {row[2]:.1f}s. Dimulai pada: {row[3]}")

        for t, zone, status in sorted(events, key=lambda x: x[0]):
            print(f"{t:%H:%M:%S} {zone:12} {status}")

        print()
        print("masuk keluar")
        for pas in detect_passages(property_eps, ambang):
            print(f"{pas['wall']:%H:%M:%S} {pas['kind']}")

    con.close()


if __name__ == "__main__":
    main()
