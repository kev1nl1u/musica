#!/usr/bin/env python3
"""Move playlist-downloaded singles back onto the album they belong to.

A Deezer playlist may reference either the album release of a track or its
standalone single release. deemix tags whatever the playlist points at, so a
track from Sentio can land tagged album='Quantum', tracknumber=1 -- and every
client that groups by tag then shows it as a one-track album of its own.

This asks Deezer whether an album or EP by the same artist contains the track,
and if so rewrites the tags and moves the file into the matching folder. Audio
data is never touched.

  album-remap.py                 report what would change
  album-remap.py --apply         write it
  album-remap.py --apply --idle-guard   skip while a download is still running

Deliberately conservative -- it would rather skip a track than file it wrong:

  - Only albums and EPs credited to one of the track's own artists are
    considered, so two songs sharing a title cannot be confused.
  - Releases whose tracklist spans more than MAX_DISTINCT_ARTISTS artists are
    treated as compilations and ignored. This also rejects genuine albums with
    many guests, which is the intended trade.
  - Matching is on the normalised title, which cannot tell a radio edit from an
    album version. Pairs that turned out wrong live in the exclusions file.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from mutagen.id3 import ID3, TALB, TPE2, TRCK

from stackconfig import ROOT
MUSIC = ROOT / "music"
BACKUP = ROOT / "deemix/remap-backup"
CACHE = ROOT / "cache/deezer.json"
EXCLUDE = ROOT / "scripts/album-remap-exclude.txt"
# Albums the dashboard's "Importa album" marks as manually-curated. Kept apart
# from the hand-edited exclusions file so the two never fight over the format.
PROTECTED_JSON = ROOT / "cache/protected-albums.json"

# A release crediting more artists than this across its tracklist is a
# compilation, not an artist's own record.
MAX_DISTINCT_ARTISTS = 3

# Anything written to the library this recently means deemix is probably still
# working through a queue. Moving files now would leave the playlist it writes
# at the end pointing at paths that no longer exist.
IDLE_SECONDS = 180

RANK = {"album": 0, "ep": 1}

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def log(msg):
    print(msg, flush=True)


def norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return NON_ALNUM.sub("", text.lower())


def safe(name):
    cleaned = ILLEGAL.sub("_", name).strip(" .")
    while len(cleaned.encode()) > 180:
        cleaned = cleaned[:-1]
    return cleaned


def padded(pos, total):
    # deemix: paddingSize 0 derives the width from the track count, and
    # padSingleDigit bumps a width of 1 up to 2.
    return str(pos).zfill(max(len(str(total or pos)), 2))


# --- Deezer -----------------------------------------------------------------


class Deezer:
    def __init__(self):
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.fetched = 0

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        data = {}
        for _ in range(3):
            try:
                with urllib.request.urlopen(url, timeout=25) as r:
                    data = json.load(r)
                break
            except Exception:
                time.sleep(1.5)
        self.cache[url] = data
        self.fetched += 1
        time.sleep(0.08)
        return data

    def save(self):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.cache))

    def artist_id(self, name):
        q = urllib.parse.quote(name)
        found = self.get(f"https://api.deezer.com/search/artist?q={q}&limit=5")
        return next(
            (a["id"] for a in found.get("data", []) if norm(a["name"]) == norm(name)),
            None,
        )

    def index(self, artist_id):
        """{normalised title: candidate} over the artist's albums and EPs."""
        out = {}
        albums = self.get(
            f"https://api.deezer.com/artist/{artist_id}/albums?limit=200"
        ).get("data", [])
        for a in albums:
            if a.get("record_type") not in RANK:
                continue
            full = self.get(f"https://api.deezer.com/album/{a['id']}")
            if not full or full.get("error"):
                continue
            tracks = full.get("tracks", {}).get("data", [])
            artists = {norm(t.get("artist", {}).get("name", "")) for t in tracks}
            if len(artists) > MAX_DISTINCT_ARTISTS:
                continue
            for pos, tr in enumerate(tracks, 1):
                cand = {
                    "rank": RANK[a["record_type"]],
                    "date": full.get("release_date") or "9999",
                    "album": full["title"],
                    "album_artist": full.get("artist", {}).get("name", ""),
                    "pos": pos,
                    "total": full.get("nb_tracks"),
                    "rtype": a["record_type"],
                }
                key = norm(tr["title"])
                if key not in out or self._better(cand, out[key]):
                    out[key] = cand
        return out

    @staticmethod
    def _better(a, b):
        return (a["rank"], a["date"]) < (b["rank"], b["date"])


# --- library ----------------------------------------------------------------


def read_tags(path):
    try:
        t = ID3(path)
    except Exception:
        return None
    first = lambda k: str(t[k].text[0]) if k in t and t[k].text else ""
    return {
        "title": first("TIT2"),
        "artist": first("TPE1"),
        "album": first("TALB"),
        "album_artist": first("TPE2"),
    }


def artists_of(tags):
    """Every artist the track is credited to.

    deemix joins multiple artists with '/' in TPE1 and ', ' in TPE2, so both have
    to be split -- keying on the raw string treats 'Martin Garrix/ZEDD' as an
    artist who does not exist.
    """
    names = [x.strip() for x in tags["artist"].split("/") if x.strip()]
    for x in tags["album_artist"].split(","):
        x = x.strip()
        if x and x not in names:
            names.append(x)
    return names


def load_exclusions():
    """Two kinds of line in the exclusions file:

      Wrong Album -> Studio Album   a specific mismatch never to make
      Some Album (Live)             a whole album never to remap at all

    The second protects a manually-curated album (e.g. a Live that only exists
    off Deezer) whose track titles would otherwise match the artist's studio
    releases and get scattered into them.
    """
    pairs, protected = set(), set()
    if not EXCLUDE.exists():
        return pairs, protected
    for line in EXCLUDE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "->" in line:
            cur, new = line.split("->", 1)
            pairs.add((norm(cur), norm(new)))
        else:
            protected.add(norm(line))
    # Albums protected from the dashboard's "Importa album".
    if PROTECTED_JSON.exists():
        try:
            for nm in json.loads(PROTECTED_JSON.read_text()):
                protected.add(norm(nm))
        except Exception:
            pass
    return pairs, protected


def target_for(entry):
    artist = safe(entry["album_artist"])
    album = safe(entry["new_album"])
    stem = safe(f"{padded(entry['pos'], entry['total'])} - {entry['title']}")
    return MUSIC / artist / album / f"{stem}.mp3"


def build_plan(dz):
    excluded, protected = load_exclusions()
    indexes = {}
    plan = []

    for path in sorted(MUSIC.rglob("*.mp3")):
        tags = read_tags(path)
        if not tags or not tags["title"]:
            continue
        # A manually-curated album: never touch it, whatever Deezer says.
        if norm(tags["album"]) in protected:
            continue

        best = None
        for name in artists_of(tags):
            aid = dz.artist_id(name)
            if not aid:
                continue
            if aid not in indexes:
                indexes[aid] = dz.index(aid)
            cand = indexes[aid].get(norm(tags["title"]))
            if cand and (best is None or Deezer._better(cand, best)):
                best = cand

        if not best or norm(best["album"]) == norm(tags["album"]):
            continue
        if (norm(tags["album"]), norm(best["album"])) in excluded:
            continue

        plan.append(
            {
                "path": str(path),
                "title": tags["title"],
                "artist": tags["artist"],
                "cur_album": tags["album"],
                "new_album": best["album"],
                "album_artist": best["album_artist"],
                "pos": best["pos"],
                "total": best["total"],
                "rtype": best["rtype"],
            }
        )
    return plan


def rewrite_playlists(moves, apply):
    """Repoint every m3u8 line at the file's new location."""
    touched = []
    for m3u8 in sorted(MUSIC.glob("*.m3u8")):
        lines = m3u8.read_text(errors="replace").splitlines()
        out, hits = [], 0
        for line in lines:
            key = line.strip()
            if key and not key.startswith("#") and key in moves:
                out.append(moves[key])
                hits += 1
            else:
                out.append(line)
        if hits:
            touched.append((m3u8.name, hits))
            if apply:
                m3u8.write_text("\n".join(out) + "\n")
    return touched


def library_is_busy():
    newest = max(
        (p.stat().st_mtime for p in MUSIC.rglob("*") if p.is_file()), default=0
    )
    return (time.time() - newest) < IDLE_SECONDS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument(
        "--idle-guard",
        action="store_true",
        help="do nothing while the library is still being written to",
    )
    args = ap.parse_args()

    if args.idle_guard and library_is_busy():
        log(f"download in corso (modifiche negli ultimi {IDLE_SECONDS}s), salto")
        return 0

    dz = Deezer()
    try:
        plan = build_plan(dz)
    finally:
        dz.save()

    moves, planned, skipped = {}, [], []
    for e in plan:
        src = Path(e["path"])
        dst = target_for(e)
        if dst.exists() and dst != src:
            skipped.append(f"destinazione occupata: {dst.relative_to(MUSIC)}")
            continue
        planned.append((e, src, dst))
        moves[str(src.relative_to(MUSIC))] = str(dst.relative_to(MUSIC))

    if not planned:
        log(f"niente da fare ({dz.fetched} richieste a Deezer)")
        return 0

    log(f"{'APPLICO' if args.apply else 'DRY RUN'} — {len(planned)} file")
    for e, src, dst in sorted(planned, key=lambda r: (r[0]["new_album"], r[0]["pos"])):
        log(f"  {src.relative_to(MUSIC)}")
        log(f"     {e['cur_album']!r} -> {e['new_album']!r} tr {e['pos']}/{e['total']}")
        log(f"     -> {dst.relative_to(MUSIC)}")

    touched = rewrite_playlists(moves, args.apply)
    for name, hits in touched:
        log(f"  playlist {name}: {hits} righe")
    for s in skipped:
        log(f"  saltato: {s}")

    if not args.apply:
        log("nessuna modifica scritta")
        return 0

    BACKUP.mkdir(parents=True, exist_ok=True)
    for e, src, dst in planned:
        shutil.copy2(src, BACKUP / src.name)
        tags = ID3(src)
        tags.setall("TALB", [TALB(encoding=1, text=[e["new_album"]])])
        tags.setall("TRCK", [TRCK(encoding=1, text=[f"{e['pos']}/{e['total']}"])])
        # Navidrome keys an album on (name, album artist). A single release
        # carries that track's collaborators here, so leaving it alone splits one
        # album into an entry per guest line-up. Track artists stay in TPE1.
        tags.setall("TPE2", [TPE2(encoding=1, text=[e["album_artist"]])])
        tags.save(src, v2_version=3)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    for d in sorted(MUSIC.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    log(f"fatto: {len(planned)} file, backup in {BACKUP}")

    # Moved files leave their old paths behind in the database, and only a full
    # scan purges those (ND_SCANNER_PURGEMISSING is 'full').
    try:
        subprocess.run(
            ["docker", "exec", "navidrome", "/app/navidrome", "scan", "--full"],
            check=True,
            capture_output=True,
            timeout=300,
        )
        log("Navidrome: full scan completato")
    except Exception as e:
        log(f"Navidrome: scan fallito ({e}) -- lancialo a mano")

    return 0


if __name__ == "__main__":
    sys.exit(main())
