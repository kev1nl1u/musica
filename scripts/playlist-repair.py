#!/usr/bin/env python3
"""Rebuild playlist files from Deezer, using whatever the library actually holds.

deemix writes an m3u8 once, when the playlist job finishes, listing only the
tracks that downloaded successfully. A track that failed at the time and was
fetched by hand later stays orphaned: nothing ever adds it to the playlist.
Files moved afterwards (see album-remap.py) are a second way for the list to
drift away from the library.

So rather than patch the m3u8, regenerate it: take the track order from Deezer,
look each track up in the library by its tags, and write out the ones that are
present. That fixes late arrivals and moves in one pass, and reports what is
still genuinely missing.

  playlist-repair.py            report what would change
  playlist-repair.py --apply    write it

A playlist is matched to its m3u8 by title, and ties are broken by comparing
track lists -- the account has several playlists sharing a name.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

from mutagen.id3 import ID3

from stackconfig import ROOT, DEEZER_USER
MUSIC = ROOT / "music"
CACHE = ROOT / "cache/deezer.json"

# DEEZER_USER: whose public playlists are reconciled. Set in config.env (or the
# environment); the public API exposes the list without a token.

# Deezer pages playlist tracks at 25.
MAX_PAGES = 40

# deemix writes a playlist's m3u8 only once the whole job is done. Rewriting it
# while that is still pending would just be overwritten a moment later.
IDLE_SECONDS = 180

# Tracks the owner has chosen to drop from a playlist: a Deezer entry that will
# never be in the library (unavailable, or simply unwanted). Keyed by Deezer
# playlist id -> list of normalised 'artist title' keys. Without this a track
# that cannot be downloaded would be reported missing forever.
IGNORE = ROOT / "cache/playlist-ignore.json"

NON_ALNUM = re.compile(r"[^a-z0-9]+")


def log(msg):
    print(msg, flush=True)


def norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return NON_ALNUM.sub("", text.lower())


def track_key(artist, title):
    return norm(f"{artist} {title}")


def load_ignore():
    if IGNORE.exists():
        try:
            return json.loads(IGNORE.read_text())
        except Exception:
            pass
    return {}


def save_ignore(data):
    IGNORE.parent.mkdir(parents=True, exist_ok=True)
    IGNORE.write_text(json.dumps(data, indent=1))


class Deezer:
    def __init__(self):
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    def get(self, url, fresh=False):
        if not fresh and url in self.cache:
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
        time.sleep(0.08)
        return data

    def save(self):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.cache))

    def playlists(self):
        # Always fresh: a cached list would hide playlists added since.
        out = []
        url = f"https://api.deezer.com/user/{DEEZER_USER}/playlists?limit=100"
        for _ in range(5):
            page = self.get(url, fresh=True)
            out.extend(page.get("data", []))
            url = page.get("next")
            if not url:
                break
        return out

    def tracks(self, playlist_id):
        """[(artist, title)] in playlist order."""
        out = []
        data = self.get(
            f"https://api.deezer.com/playlist/{playlist_id}", fresh=True
        )
        tracks = data.get("tracks", {})
        for _ in range(MAX_PAGES):
            for t in tracks.get("data", []):
                out.append((t.get("artist", {}).get("name", ""), t.get("title", "")))
            nxt = tracks.get("next")
            if not nxt:
                break
            tracks = self.get(nxt, fresh=True)
        return out


def library_index():
    """Two lookups over the library: artist+title, and title alone."""
    exact, loose = {}, {}
    for path in sorted(MUSIC.rglob("*.mp3")):
        try:
            tags = ID3(path)
        except Exception:
            continue
        title = str(tags["TIT2"].text[0]) if "TIT2" in tags else ""
        artist = str(tags["TPE1"].text[0]) if "TPE1" in tags else ""
        if not title:
            continue
        rel = str(path.relative_to(MUSIC))
        for name in [a.strip() for a in artist.split("/") if a.strip()] or [""]:
            exact.setdefault(norm(f"{name} {title}"), rel)
        loose.setdefault(norm(title), rel)
    return exact, loose


def find(exact, loose, artist, title):
    return exact.get(norm(f"{artist} {title}")) or loose.get(norm(title))


def pick_playlist(m3u8, candidates, dz, exact, loose):
    """The Deezer playlist this file came from.

    Title alone is ambiguous -- the account has two 'DANCE' and two 'Club peak'
    -- so where several share a name, prefer the one whose tracks best cover
    what the file already lists.
    """
    # Exact title first: names made only of symbols or emoji ('🔊', '✚✖︎', '.')
    # all normalise to the empty string, so norm-matching would conflate them.
    named = [p for p in candidates if p["title"] == m3u8.stem]
    if not named:
        stem = norm(m3u8.stem)
        named = [p for p in candidates if norm(p["title"]) == stem]
    if len(named) <= 1:
        return named[0] if named else None

    current = {
        norm(Path(l.strip()).stem)
        for l in m3u8.read_text(errors="replace").splitlines()
        if l.strip() and not l.startswith("#")
    }
    best, best_score = None, -1.0
    for p in named:
        entries = {norm(t) for _, t in dz.tracks(p["id"])}
        hits = sum(1 for c in current if any(c.endswith(e) or e in c for e in entries))
        score = hits / max(len(current), 1)
        if score > best_score:
            best, best_score = p, score
    return best


def resolve_by_name(name, candidates, dz, exact, loose):
    """The (m3u8, Deezer playlist) pair for a playlist name, or (None, None).

    The dashboard passes the exact display name, which is the m3u8 stem, so match
    on that directly. norm() would collapse every symbol-only name to '' and
    return whichever file sorts first.
    """
    files = list(MUSIC.glob("*.m3u8"))
    matches = [m for m in files if m.stem == name]
    if not matches:
        matches = [m for m in sorted(files) if norm(m.stem) == norm(name)]
    for m3u8 in matches:
        pl = pick_playlist(m3u8, candidates, dz, exact, loose)
        if pl:
            return m3u8, pl
    return None, None


def status_for(name, dz, candidates, exact, loose):
    """Every Deezer track of a playlist, tagged present / missing / excluded."""
    m3u8, pl = resolve_by_name(name, candidates, dz, exact, loose)
    if not pl:
        return None

    ignored = set(load_ignore().get(str(pl["id"]), []))
    tracks = []
    present = missing = excluded = 0
    for artist, title in dz.tracks(pl["id"]):
        key = track_key(artist, title)
        rel = find(exact, loose, artist, title)
        if key in ignored:
            state = "excluded"
            excluded += 1
        elif rel:
            state = "present"
            present += 1
        else:
            state = "missing"
            missing += 1
        tracks.append(
            {"artist": artist, "title": title, "key": key,
             "state": state, "path": rel}
        )

    return {
        "name": name,
        "pid": pl["id"],
        "total": len(tracks),
        "present": present,
        "missing": missing,
        "excluded": excluded,
        "complete": missing == 0,
        "tracks": tracks,
    }


def overview(dz, candidates, exact, loose):
    """Per synced playlist: present/missing/excluded counts and the missing list.

    One pass over every m3u8 in the library -- the dashboard uses it both to show
    'scaricati / tutti' in the playlist table and to match an uploaded file to a
    missing track, without spawning a process (and re-reading the whole library)
    per playlist.
    """
    ignore = load_ignore()
    out = []
    for m3u8 in sorted(MUSIC.glob("*.m3u8")):
        pl = pick_playlist(m3u8, candidates, dz, exact, loose)
        if not pl:
            continue
        ignored = set(ignore.get(str(pl["id"]), []))
        present = excluded = 0
        missing = []
        for artist, title in dz.tracks(pl["id"]):
            key = track_key(artist, title)
            if key in ignored:
                excluded += 1
            elif find(exact, loose, artist, title):
                present += 1
            else:
                missing.append({"artist": artist, "title": title, "key": key})
        out.append({
            "name": m3u8.stem, "pid": pl["id"],
            "present": present, "missing": len(missing),
            "excluded": excluded, "total": present + len(missing),
            "missing_tracks": missing,
        })
    return out


def set_ignore(name, key, on, dz, candidates, exact, loose):
    _, pl = resolve_by_name(name, candidates, dz, exact, loose)
    if not pl:
        return False
    data = load_ignore()
    pid = str(pl["id"])
    keys = set(data.get(pid, []))
    keys.add(key) if on else keys.discard(key)
    if keys:
        data[pid] = sorted(keys)
    else:
        data.pop(pid, None)
    save_ignore(data)
    return True


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
    ap.add_argument("--status", metavar="NAME",
                    help="print one playlist's tracks as JSON and exit")
    ap.add_argument("--overview", action="store_true",
                    help="counts + missing tracks for every playlist, as JSON")
    ap.add_argument("--ignore", nargs=2, metavar=("NAME", "KEY"),
                    help="drop a track from a playlist's expected set")
    ap.add_argument("--unignore", nargs=2, metavar=("NAME", "KEY"),
                    help="undo --ignore")
    args = ap.parse_args()

    # Read-only / config subcommands used by the dashboard. They resolve
    # playlists against Deezer just like a repair does, but touch no m3u8.
    if args.status or args.overview or args.ignore or args.unignore:
        dz = Deezer()
        try:
            candidates = dz.playlists()
            exact, loose = library_index()
            if args.overview:
                print(json.dumps(overview(dz, candidates, exact, loose)))
                return 0
            if args.ignore or args.unignore:
                name, key = args.ignore or args.unignore
                ok = set_ignore(name, key, bool(args.ignore),
                                dz, candidates, exact, loose)
                print(json.dumps({"ok": ok}))
                return 0 if ok else 1
            st = status_for(args.status, dz, candidates, exact, loose)
            print(json.dumps(st) if st else json.dumps({"error": "not found"}))
            return 0 if st else 1
        finally:
            dz.save()

    if args.idle_guard and library_is_busy():
        log(f"download in corso (modifiche negli ultimi {IDLE_SECONDS}s), salto")
        return 0

    changed = False
    dz = Deezer()
    try:
        candidates = dz.playlists()
        if not candidates:
            log("nessuna playlist leggibile dall'account Deezer")
            return 1

        exact, loose = library_index()
        ignore = load_ignore()
        log(f"libreria: {len(loose)} titoli, playlist su Deezer: {len(candidates)}\n")

        for m3u8 in sorted(MUSIC.glob("*.m3u8")):
            pl = pick_playlist(m3u8, candidates, dz, exact, loose)
            if not pl:
                log(f"{m3u8.name}: nessuna playlist Deezer con questo nome, salto")
                continue

            wanted = dz.tracks(pl["id"])
            ignored = set(ignore.get(str(pl["id"]), []))
            lines, missing, skipped = [], [], 0
            for artist, title in wanted:
                # An excluded track counts neither way: the owner has said it is
                # not coming, so it must not keep the playlist looking incomplete.
                if track_key(artist, title) in ignored:
                    skipped += 1
                    continue
                rel = find(exact, loose, artist, title)
                if rel:
                    lines.append(rel)
                else:
                    missing.append(f"{artist} - {title}")

            # Same tracks in the same order means there is nothing to do.
            old = [
                l.strip()
                for l in m3u8.read_text(errors="replace").splitlines()
                if l.strip() and not l.startswith("#")
            ]
            added = [l for l in lines if l not in old]
            dropped = [l for l in old if l not in lines]

            log(f"{m3u8.name}  (Deezer {pl['id']}, {len(wanted)} tracce)")
            extra = f"   esclusi: {skipped}" if skipped else ""
            log(f"   presenti in libreria: {len(lines)}   mancanti: {len(missing)}{extra}")
            for a in added:
                log(f"   + {a}")
            for d in dropped:
                log(f"   - {d}")
            for m in missing:
                log(f"   ! non in libreria: {m}")

            if lines != old and args.apply:
                m3u8.write_text("\n".join(lines) + "\n")
                changed = True
                log("   riscritto")
            elif lines == old:
                log("   già allineato")
            log("")
    finally:
        dz.save()

    if not args.apply:
        log("nessuna modifica scritta")
        return 0

    if changed:
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
