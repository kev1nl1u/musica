#!/usr/bin/env python3
"""Give deemix playlist files their real name.

deemix writes every playlist as the literal '%playlist%.m3u8' -- it does not
substitute that template. With a download queue that causes two problems:

  1. Each finished playlist overwrites the previous file, so a playlist can be
     lost outright before anything has a chance to read it.
  2. The name has to come from somewhere. The log carries the playlist id as
     '[playlist_<id>_<n>]', but with several downloads interleaved the most
     recent id in the log is not necessarily the one that produced the file.

So: watch with inotify and move the file out of the music folder the moment it
is closed (fixes 1), then work out which playlist it belongs to by matching its
track list against the Deezer API (fixes 2). Matching on content rather than on
log order means interleaved downloads cannot mix names up.

Overwriting an existing playlist file is intended -- re-downloading a playlist
should refresh it in place rather than pile up copies.
"""

import ctypes
import ctypes.util
import errno
import json
import os
import re
import struct
import time
import unicodedata
import urllib.request
from pathlib import Path

from stackconfig import ROOT
MUSIC = ROOT / "music"
PENDING = ROOT / "deemix/pending"
LOGS = ROOT / "deemix/config/logs"

STRAY_NAME = "%playlist%.m3u8"

# '[playlist_9247435102_3] Artist - Title :: Getting tags.'
PLAYLIST_TAG = re.compile(r"\[playlist_(\d+)_\d+\]")

ILLEGAL = re.compile(r"[/\x00-\x1f]")
NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Leading track number written by albumTracknameTemplate: '01 - Title'.
TRACK_PREFIX = re.compile(r"^\d+\s*[-.]?\s*")

# Fraction of the file's tracks that must appear in a candidate playlist.
MATCH_THRESHOLD = 0.5
# How many log files back to look for candidate ids.
LOG_LOOKBACK = 3
# Deezer pages tracks at 25; cap the walk so a huge playlist cannot stall us.
MAX_TRACK_PAGES = 8

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
EVENT_HEADER = struct.Struct("iIII")  # wd, mask, cookie, len


def log(msg):
    print(msg, flush=True)


# --- matching ---------------------------------------------------------------


def normalise(text):
    """Fold to bare lowercase alphanumerics so punctuation cannot break a match."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return NON_ALNUM.sub("", text.lower())


def keys_for(artist, title):
    """Keys a track may be recognised by.

    The title-only key is what carries a match when the artist could not be
    recovered from the path; keeping both sides symmetric means the score stays
    a straight ratio either way.
    """
    keys = set()
    full = normalise(f"{artist} {title}")
    bare = normalise(title)
    if full:
        keys.add(full)
    if bare:
        keys.add(bare)
    return keys


def split_entry(line):
    """(artist, title) for one m3u8 line, whichever layout wrote it.

    Flat:       'Artist - Title.mp3'
    Structured: 'Artist/Album/01 - Title.mp3'
    """
    parts = Path(line).parts
    stem = Path(line).stem

    if len(parts) >= 3:
        # Artist folder, then album; the number prefix is not part of the title.
        return parts[-3], TRACK_PREFIX.sub("", stem)

    artist, sep, title = stem.partition(" - ")
    if sep:
        return artist, title
    return "", stem


def tracks_in_file(path):
    entries = set()
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries |= keys_for(*split_entry(line))
    return entries


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def deezer_playlist(playlist_id):
    """Return (title, {track keys}) for a Deezer playlist."""
    data = fetch_json(f"https://api.deezer.com/playlist/{playlist_id}")
    if data.get("error"):
        raise RuntimeError(data["error"])

    title = (data.get("title") or "").strip()
    entries = set()
    tracks = data.get("tracks", {})
    pages = 0
    while True:
        for t in tracks.get("data", []):
            artist = (t.get("artist") or {}).get("name", "")
            entries |= keys_for(artist, t.get("title", ""))
        nxt = tracks.get("next")
        pages += 1
        if not nxt or pages >= MAX_TRACK_PAGES:
            break
        tracks = fetch_json(nxt)

    return title, entries


def candidate_ids():
    """Playlist ids seen in recent logs, most recently mentioned first."""
    logs = sorted(LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    seen = []
    for path in logs[:LOG_LOOKBACK]:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for pid in reversed(PLAYLIST_TAG.findall(text)):
            if pid not in seen:
                seen.append(pid)
    return seen


def identify(path):
    """Best-matching (title, id) for a playlist file, or (None, None)."""
    wanted = tracks_in_file(path)
    if not wanted:
        return None, None

    best = (0.0, None, None)
    for pid in candidate_ids():
        try:
            title, entries = deezer_playlist(pid)
        except Exception as e:
            log(f"  lookup failed for playlist {pid}: {e}")
            continue
        if not entries:
            continue
        score = len(wanted & entries) / len(wanted)
        log(f"  playlist {pid} {title!r}: {score:.0%} match")
        if score > best[0]:
            best = (score, title, pid)
        if score == 1.0:
            break

    score, title, pid = best
    if title and score >= MATCH_THRESHOLD:
        return title, pid
    return None, None


# --- file handling ----------------------------------------------------------


def safe_name(title):
    cleaned = ILLEGAL.sub("_", title).strip(" .")
    # 200 bytes leaves room for the extension inside ext4's 255-byte limit.
    while len(cleaned.encode()) > 200:
        cleaned = cleaned[:-1]
    return cleaned


def quarantine():
    """Move the stray file out of the music folder before deemix overwrites it.

    Returns the new path, or None if there was nothing to take.
    """
    stray = MUSIC / STRAY_NAME
    holding = PENDING / f"{time.time_ns()}.m3u8"
    try:
        stray.rename(holding)
    except FileNotFoundError:
        return None
    except OSError as e:
        log(f"could not quarantine: {e}")
        return None
    return holding


def resolve(holding):
    title, pid = identify(holding)
    if not title:
        log(f"no confident match for {holding.name}, leaving it in {PENDING}")
        return

    target = MUSIC / f"{safe_name(title)}.m3u8"
    holding.replace(target)
    log(f"{holding.name} -> {target.name} (playlist {pid})")


def drain():
    """Resolve anything sitting in the holding area, oldest first."""
    for holding in sorted(PENDING.glob("*.m3u8")):
        try:
            resolve(holding)
        except Exception as e:
            log(f"failed to resolve {holding.name}: {e}")


# --- inotify ----------------------------------------------------------------


def watch():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    fd = libc.inotify_init1(0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")

    wd = libc.inotify_add_watch(
        fd, str(MUSIC).encode(), IN_CLOSE_WRITE | IN_MOVED_TO
    )
    if wd < 0:
        raise OSError(ctypes.get_errno(), f"cannot watch {MUSIC}")

    log(f"watching {MUSIC} for {STRAY_NAME}")
    buf = b""
    while True:
        try:
            buf += os.read(fd, 8192)
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise

        hits = False
        while len(buf) >= EVENT_HEADER.size:
            _, _, _, length = EVENT_HEADER.unpack(buf[: EVENT_HEADER.size])
            end = EVENT_HEADER.size + length
            if len(buf) < end:
                break
            name = buf[EVENT_HEADER.size : end].split(b"\0", 1)[0].decode(
                errors="replace"
            )
            buf = buf[end:]
            if name == STRAY_NAME:
                hits = True

        if hits:
            # Grab it first, ask questions later -- the next playlist in the
            # queue may be about to write over this exact filename.
            holding = quarantine()
            if holding:
                log(f"caught {STRAY_NAME} -> {holding.name}")
                drain()


def main():
    PENDING.mkdir(parents=True, exist_ok=True)
    # Anything left from a previous run, plus a file already sitting there.
    quarantine()
    drain()
    while True:
        try:
            watch()
        except Exception as e:
            log(f"watcher died ({e}), restarting in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
