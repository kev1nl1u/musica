#!/usr/bin/env python3
"""Cut a downloaded playlist loose from its m3u8 so it can be edited.

A playlist imported from a file is marked sync=1, and Navidrome re-imports it
from that file on every full scan -- so anything added from Symfonium survives
only until the next scan. Deezer stays the source of truth whether you want it
to or not.

Detaching ends that: the m3u8 moves out of the library and the database row
loses its sync flag and path, leaving a playlist identical to one created by
hand. Navidrome never touches it again, and it becomes yours to edit.

  playlist-detach.py                  list playlists and their state
  playlist-detach.py NAME [NAME...]   detach these
  playlist-detach.py --all            detach every synced playlist

Detach once the download has settled: afterwards nothing tops the playlist up,
which is the point. Tracks that failed and were fetched later have to be added
by hand -- run playlist-repair.py first if you would rather it catch them.

Navidrome is stopped for the update. SQLite tolerates a concurrent writer, but
the server caches playlists in memory and would not see the change.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from stackconfig import ROOT
MUSIC = ROOT / "music"
DB = ROOT / "navidrome/data/navidrome.db"
ARCHIVE = ROOT / "deemix/playlists-detached"


def log(msg):
    print(msg, flush=True)


def playlists(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return list(
            con.execute("SELECT id, name, song_count, sync, path FROM playlist")
        )
    finally:
        con.close()


def navidrome(action):
    subprocess.run(["docker", action, "navidrome"], check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="playlists to detach")
    ap.add_argument("--all", action="store_true", help="detach every synced playlist")
    ap.add_argument("--drop", action="store_true",
                    help="delete the playlist outright instead of detaching")
    args = ap.parse_args()

    rows = playlists(DB)
    if not args.names and not args.all:
        log(f"{'playlist':32} {'tracce':>7}  stato")
        for _id, name, count, sync, path in rows:
            state = "sincronizzata" if sync else "propria (modificabile)"
            if sync and path and not (MUSIC / Path(path).name).exists():
                state = "sincronizzata, ma il file non c'e piu"
            log(f"{name:32} {count:7}  {state}")
        log("\nPassa un nome per staccarla, o --all per tutte.")
        return 0

    wanted = [r for r in rows if args.all or r[1] in args.names]
    if not wanted:
        log("nessuna playlist con quel nome")
        return 1
    if args.all:
        wanted = [r for r in wanted if r[3]]

    for _id, name, count, sync, path in wanted:
        log(f"{name!r}: {count} tracce, sync={sync}, path={path}")
    log("")

    # Navidrome runs as root in its container, so the database it creates is
    # root-owned. Fail here rather than half way through, with the server down.
    if not os.access(DB, os.W_OK):
        log(f"{DB} non scrivibile -- rilancia con sudo")
        return 1

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    navidrome("stop")
    try:
        con = sqlite3.connect(DB)
        try:
            for pid, name, count, sync, path in wanted:
                if args.drop:
                    con.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
                    con.execute("DELETE FROM playlist WHERE id=?", (pid,))
                    log(f"{name!r}: rimossa dal database")
                    continue

                # The file leaves the library so the scanner stops finding it,
                # but is kept: it is the only record of the original order.
                if path:
                    src = MUSIC / Path(path).name
                    if src.exists():
                        shutil.move(str(src), str(ARCHIVE / src.name))
                        log(f"{name!r}: m3u8 archiviato in {ARCHIVE}")
                con.execute(
                    "UPDATE playlist SET sync=0, path='' WHERE id=?", (pid,)
                )
                log(f"{name!r}: staccata, ora modificabile")
            con.commit()
        finally:
            con.close()
    finally:
        navidrome("start")
        time.sleep(4)

    log("\nstato finale:")
    for _id, name, count, sync, path in playlists(DB):
        log(f"   {name:32} {count:5}  {'sincronizzata' if sync else 'propria'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
