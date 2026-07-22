#!/usr/bin/env python3
"""A small control panel for the music stack.

Serves a single page plus a JSON API over the standard library only -- no
framework, so nothing to install on the box. It reads state (library stats,
playlists, the deemix queue, the systemd timer) and runs the maintenance
scripts as background jobs whose output can be polled.

The point of it is detaching playlists from a browser instead of the CLI, so
that is the centrepiece; everything else is there because the same page may as
well show it.

Privileged only in one place: playlist-detach writes Navidrome's root-owned
database, so it runs through sudo (see scripts/music-dashboard.sudoers). Every
other script writes files owned by this user. The whole thing sits behind
oauth2-proxy, so reaching it at all means having logged in.
"""

import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mutagen
from mutagen.id3 import ID3, TIT2, TPE1

from stackconfig import ROOT, NAVIDROME_URL, DEEMIX_URL
MUSIC = ROOT / "music"
DB = ROOT / "navidrome/data/navidrome.db"
QUEUE = ROOT / "deemix/config/queue"
SCRIPTS = ROOT / "scripts"
HTML = SCRIPTS / "dashboard.html"

HOST, PORT = "127.0.0.1", 8765

# Only these can be launched, and only with these argument shapes. The web
# layer never passes a free-form command line -- it names an action here.
ACTIONS = {
    "remap-dry": {
        "label": "Album remap (anteprima)",
        "argv": ["python3", str(SCRIPTS / "album-remap.py")],
    },
    "remap-apply": {
        "label": "Album remap (applica)",
        "argv": ["python3", str(SCRIPTS / "album-remap.py"), "--apply"],
    },
    "repair-dry": {
        "label": "Playlist repair (anteprima)",
        "argv": ["python3", str(SCRIPTS / "playlist-repair.py")],
    },
    "repair-apply": {
        "label": "Playlist repair (applica)",
        "argv": ["python3", str(SCRIPTS / "playlist-repair.py"), "--apply"],
    },
}


# --- jobs -------------------------------------------------------------------

jobs = {}
jobs_lock = threading.Lock()


def run_job(label, argv):
    """Spawn argv in the background; return a job id to poll."""
    jid = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[jid] = {
            "id": jid,
            "label": label,
            "cmd": " ".join(shlex.quote(a) for a in argv),
            "status": "running",
            "started": time.time(),
            "finished": None,
            "returncode": None,
            "output": "",
        }

    def worker():
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                with jobs_lock:
                    jobs[jid]["output"] += line
            proc.wait()
            with jobs_lock:
                jobs[jid]["status"] = "done" if proc.returncode == 0 else "failed"
                jobs[jid]["returncode"] = proc.returncode
                jobs[jid]["finished"] = time.time()
        except Exception as e:  # a spawn that never started
            with jobs_lock:
                jobs[jid]["status"] = "failed"
                jobs[jid]["output"] += f"\n[dashboard] {e}\n"
                jobs[jid]["finished"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return jid


# --- state readers ----------------------------------------------------------


def db_query(sql, params=()):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def stats():
    tracks = db_query("SELECT count(*) n FROM media_file")[0]["n"]
    albums = db_query("SELECT count(*) n FROM album")[0]["n"]
    multi = db_query("SELECT count(*) n FROM album WHERE song_count>1")[0]["n"]
    artists = db_query("SELECT count(DISTINCT album_artist) n FROM album")[0]["n"]
    size = sum(p.stat().st_size for p in MUSIC.rglob("*.mp3"))
    return {
        "tracks": tracks,
        "albums": albums,
        "albums_multi": multi,
        "albums_single": albums - multi,
        "artists": artists,
        "size_gb": round(size / 1e9, 2),
    }


def playlists():
    rows = db_query(
        "SELECT name, song_count, sync, path FROM playlist ORDER BY sync DESC, name"
    )
    for r in rows:
        r["synced"] = bool(r["sync"])
        fname = Path(r["path"]).name if r["path"] else ""
        r["file_present"] = bool(fname) and (MUSIC / fname).exists()
    return rows


def queue():
    if not QUEUE.exists():
        return []
    order = []
    order_file = QUEUE / "order.json"
    if order_file.exists():
        try:
            order = json.loads(order_file.read_text())
        except Exception:
            order = []
    # A job records the tracks it failed to download. One fetched by hand
    # afterwards is now in the library, but deemix leaves the failure in the
    # job file forever -- hide those resolved lines, same as errors.txt.
    stems = [_norm(p.stem) for p in MUSIC.rglob("*.mp3")]
    out = []
    for f in sorted(QUEUE.glob("*.json")):
        if f.name == "order.json":
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        errors, resolved = [], 0
        for e in d.get("errors", []) or []:
            data = e.get("data", {}) if isinstance(e, dict) else {}
            title = data.get("title", "")
            nt = _norm(title)
            if nt and any(nt in s for s in stems):
                resolved += 1
                continue
            errors.append(
                {
                    "track": f"{data.get('artist', '')} - {title}".strip(" -"),
                    "message": e.get("message", "") if isinstance(e, dict) else str(e),
                }
            )
        out.append(
            {
                "uuid": d.get("uuid", f.stem),
                "title": d.get("title", "?"),
                "artist": d.get("artist", ""),
                "type": d.get("type", ""),
                "size": d.get("size", 0),
                "downloaded": d.get("downloaded", 0),
                "failed": max(0, d.get("failed", 0) - resolved),
                "resolved": resolved,
                "status": d.get("status", ""),
                "in_order": d.get("uuid", f.stem) in order,
                "errors": errors,
            }
        )
    return out


def deemix_errors():
    """Failed tracks deemix recorded, plus a hung-queue check.

    A high, climbing 'Track not available' count means deemix is stuck in the
    fallback loop on an unavailable track -- the failure mode that clean fixes.
    """
    ef = MUSIC / "errors.txt"
    raw = ef.read_text(errors="replace") if ef.exists() else ""

    # deemix overwrites errors.txt only on its next download, never when a
    # failed track is later fetched by hand. So a line whose track is now in
    # the library is stale -- hide it instead of reporting a solved failure.
    # Match on the title against filename stems (no tag read, so it stays cheap
    # enough for the 15s poll).
    stems = [_norm(p.stem) for p in MUSIC.rglob("*.mp3")]
    kept, resolved = [], 0
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        title = parts[1].split(" - ", 1)[1] if len(parts) >= 2 and " - " in parts[1] else ""
        nt = _norm(title)
        if nt and any(nt in s for s in stems):
            resolved += 1
            continue
        kept.append(line)

    loop = 0
    try:
        res = subprocess.run(
            ["docker", "logs", "deemix", "--since", "15m"],
            capture_output=True, text=True, timeout=8,
        )
        loop = (res.stdout + res.stderr).count("Track not available")
    except Exception:
        pass
    return {"errors_txt": "\n".join(kept), "loop_hits": loop, "resolved_hidden": resolved}


def systemd():
    out = {}
    for unit in ("album-remap.timer", "deemix-playlist-namer.service"):
        try:
            res = subprocess.run(
                ["systemctl", "show", unit,
                 "--property=ActiveState,SubState,ExecMainStatus"],
                capture_output=True, text=True, timeout=5,
            )
            props = dict(
                l.split("=", 1) for l in res.stdout.strip().splitlines() if "=" in l
            )
            out[unit] = props
        except Exception as e:
            out[unit] = {"error": str(e)}
    try:
        res = subprocess.run(
            ["systemctl", "list-timers", "album-remap.timer",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
        out["next_run"] = res.stdout.strip().split("  ")[0] if res.stdout.strip() else ""
    except Exception:
        out["next_run"] = ""
    return out


def job_list():
    with jobs_lock:
        return sorted(
            (
                {k: v for k, v in j.items() if k != "output"}
                for j in jobs.values()
            ),
            key=lambda j: j["started"],
            reverse=True,
        )


# --- detach -----------------------------------------------------------------


def detach(name, drop=False):
    argv = ["sudo", "-n", "python3", str(SCRIPTS / "playlist-detach.py"), name]
    if drop:
        argv.append("--drop")
    return run_job(f"{'Elimina' if drop else 'Detach'} {name!r}", argv)


def clean_queue(uuid):
    argv = ["python3", str(SCRIPTS / "queue-clean.py"), uuid]
    return run_job(f"Pulisci coda {uuid}", argv)


REPAIR = str(SCRIPTS / "playlist-repair.py")


def playlist_status(name):
    """Deezer tracklist of one playlist, present/missing/excluded per track."""
    res = subprocess.run(
        ["python3", REPAIR, "--status", name],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(res.stdout)
    except Exception:
        return {"error": res.stderr.strip() or "no output"}


def overview():
    """Counts + missing tracks for every synced playlist, in one Deezer pass."""
    res = subprocess.run(
        ["python3", REPAIR, "--overview"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    try:
        return json.loads(res.stdout)
    except Exception:
        return []


def playlist_counts():
    """{playlist name: {present, total}} for the playlist table."""
    return {
        p["name"]: {"present": p["present"], "total": p["total"]}
        for p in overview()
    }


def playlist_ignore(name, key, on):
    flag = "--ignore" if on else "--unignore"
    res = subprocess.run(
        ["python3", REPAIR, flag, name, key],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(res.stdout)
    except Exception:
        return {"ok": False, "error": res.stderr.strip()}


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", text.lower())


def _safe(name):
    return (_ILLEGAL.sub("_", name).strip(" .")[:120]) or "unknown"


# --- file browser -----------------------------------------------------------
# A small explorer scoped strictly to the music library: browse folders, make
# new ones, and upload audio into them -- for material that cannot come from
# Deezer (an Apple-exclusive live album, say) and has to be filed by hand.

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".aiff"}


def _fs_path(rel):
    """Resolve a library-relative path, refusing anything outside music/."""
    base = MUSIC.resolve()
    target = (base / (rel or "").strip("/")).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def fs_list(rel):
    target = _fs_path(rel)
    if not target or not target.is_dir():
        return {"error": "cartella non valida"}
    dirs, files = [], []
    for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dirs.append(p.name)
        elif p.is_file():
            files.append({"name": p.name, "size": p.stat().st_size})
    rp = str(target.relative_to(MUSIC.resolve()))
    return {"path": "" if rp == "." else rp, "dirs": dirs, "files": files}


def fs_mkdir(rel, name):
    parent = _fs_path(rel)
    if not parent or not parent.is_dir():
        return {"error": "cartella non valida"}
    safe = _ILLEGAL.sub("_", name or "").strip(" .")[:120]
    if not safe:
        return {"error": "nome non valido"}
    d = parent / safe
    if _fs_path(str(d.relative_to(MUSIC.resolve()))) is None:
        return {"error": "percorso non valido"}
    if d.exists():
        return {"error": "esiste già"}
    d.mkdir(parents=True)
    return {"ok": True, "name": safe}


def fs_upload(rel, filename, data):
    parent = _fs_path(rel)
    if not parent or not parent.is_dir():
        return {"error": "cartella non valida"}
    base = _ILLEGAL.sub("_", Path(filename or "").name).strip(" .")
    if not base:
        return {"error": "nome file non valido"}
    if Path(base).suffix.lower() not in AUDIO_EXT:
        return {"error": f"tipo non supportato ({Path(base).suffix or '?'})"}
    dst = parent / base
    if _fs_path(str(dst.relative_to(MUSIC.resolve()))) is None:
        return {"error": "percorso non valido"}
    if dst.exists():
        return {"error": "esiste già"}
    dst.write_bytes(data)
    return {"ok": True, "name": base}


PROTECTED = ROOT / "cache/protected-albums.json"


def protect_album(name):
    """Mark an album as manually-curated so album-remap never touches it.

    An imported album exists off Deezer by definition, so its tracks would
    otherwise risk being scattered into the artist's studio releases. album-remap
    reads this same file. Idempotent, matched case/punctuation-insensitively.
    """
    name = (name or "").strip()
    if not name:
        return
    data = []
    if PROTECTED.exists():
        try:
            data = json.loads(PROTECTED.read_text())
        except Exception:
            data = []
    if any(_norm(x) == _norm(name) for x in data):
        return
    data.append(name)
    PROTECTED.parent.mkdir(parents=True, exist_ok=True)
    PROTECTED.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def fs_import(meta, filename, data):
    """Save one album track and tag it from a form -- so the user only uploads.

    Album name and album artist come once from the form; per-track title and
    number are guessed from the filename and can be edited. Tags are written
    through mutagen's format-agnostic 'easy' interface so mp3, m4a and flac all
    get title/artist/album/albumartist/tracknumber, which is what Navidrome
    groups on. Filed under Artist/Album/NN - Title.ext.
    """
    album = (meta.get("album") or "").strip()
    albumartist = (meta.get("albumartist") or "").strip()
    if not album or not albumartist:
        return {"error": "artista e titolo dell'album sono richiesti"}
    base = Path(_ILLEGAL.sub("_", Path(filename or "").name).strip(" ."))
    ext = base.suffix.lower()
    if ext not in AUDIO_EXT:
        return {"error": f"tipo non supportato ({ext or '?'})"}

    title = (meta.get("title") or base.stem).strip() or base.stem
    artist = (meta.get("artist") or albumartist).strip()
    try:
        track = int(meta.get("track") or 0)
    except ValueError:
        track = 0
    try:
        total = int(meta.get("total") or 0)
    except ValueError:
        total = 0

    width = max(2, len(str(total or track or 1)))
    nn = str(track).zfill(width) if track else ""
    stem = f"{nn} - {title}" if nn else title
    dst = MUSIC / _safe(albumartist) / _safe(album) / f"{_safe(stem)}{ext}"
    if _fs_path(str(dst.relative_to(MUSIC.resolve()))) is None:
        return {"error": "percorso non valido"}
    if dst.exists():
        return {"error": f"esiste già {dst.relative_to(MUSIC)}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)

    tagged = True
    try:
        af = mutagen.File(dst, easy=True)
        if af is None:
            raise ValueError("formato senza tag")
        af["title"] = [title]
        af["artist"] = [artist]
        af["album"] = [album]
        af["albumartist"] = [albumartist]
        if track:
            af["tracknumber"] = [f"{track}/{total}" if total else str(track)]
        af.save()
    except Exception:
        tagged = False  # placed anyway; user can retag from a client

    # An imported album is manually curated -- keep album-remap off it.
    protect_album(album)
    return {"ok": True, "path": str(dst.relative_to(MUSIC)), "tagged": tagged}


def scan_library():
    """Ask Navidrome to rescan, as a job the page can watch."""
    return run_job(
        "Scansione libreria",
        ["docker", "exec", "navidrome", "/app/navidrome", "scan", "--full"],
    )


def add_track_file(name, filename, data):
    """Add an uploaded mp3 to the library.

    If the file matches a track some playlist is missing, it is retagged to that
    track's exact artist and title and playlist-repair runs so it joins the
    playlist. If it matches nothing, it is simply filed in the library under its
    own tags -- adding a song no playlist happens to want is fine.
    """
    is_mp3 = data[:3] == b"ID3" or (
        len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    )
    if not is_mp3:
        return {"error": "non sembra un file mp3"}

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        try:
            t = ID3(tmp)
            f_title = str(t["TIT2"].text[0]) if "TIT2" in t else ""
            f_artist = str(t["TPE1"].text[0]) if "TPE1" in t else ""
            f_album = str(t["TALB"].text[0]) if "TALB" in t else ""
        except Exception:
            f_title = f_artist = f_album = ""
        if not f_title:
            f_title = Path(filename).stem

        # Tracks some playlist is missing, in scope: one named playlist, or --
        # from the home navbar -- every synced one via a single overview pass.
        missing = []  # (playlist name, track {artist,title,key})
        if name and name != "*":
            st = playlist_status(name)
            if not st.get("error"):
                missing = [(name, x) for x in st["tracks"]
                           if x["state"] == "missing"]
        else:
            for p in overview():
                for mt in p["missing_tracks"]:
                    missing.append((p["name"], mt))

        # Match the file to a missing track: exact artist+title, else a unique
        # title. No match is fine -- the file just goes to the library as-is.
        fkey = _norm(f"{f_artist} {f_title}")
        match = next((pt for pt in missing if pt[1]["key"] == fkey), None)
        if not match:
            ftitle = _norm(f_title)
            cands = [pt for pt in missing if _norm(pt[1]["title"]) == ftitle]
            if len(cands) == 1:
                match = cands[0]

        if match:
            target_playlist, track = match
            artist, title = track["artist"], track["title"]
            album = f_album or title
        else:
            target_playlist = None
            artist, title = (f_artist or "Unknown Artist"), f_title
            album = f_album or title

        dst = MUSIC / _safe(artist) / _safe(album) / f"{_safe(f'{artist} - {title}')}.mp3"
        if dst.exists():
            return {"error": f"esiste già {dst.relative_to(MUSIC)}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp, dst)
        # Tag title/artist so the library shows it right (and, when matched, so
        # repair is certain to pick it up). The album tag is left as-is.
        try:
            out = ID3(dst)
        except Exception:
            out = ID3()
        out.setall("TIT2", [TIT2(encoding=3, text=[title])])
        out.setall("TPE1", [TPE1(encoding=3, text=[artist])])
        out.save(dst, v2_version=3)
    finally:
        os.unlink(tmp)

    if target_playlist:
        # Repair (no idle guard) writes it into the m3u8 and rescans now.
        job = run_job(f"Aggiungi {artist} - {title}", ["python3", REPAIR, "--apply"])
        return {"matched": f"{artist} - {title}", "playlist": target_playlist, "job": job}
    # No playlist wanted it: just make Navidrome see the new file.
    job = run_job(
        f"Scansione ({artist} - {title})",
        ["docker", "exec", "navidrome", "/app/navidrome", "scan", "--full"],
    )
    return {"matched": f"{artist} - {title}", "playlist": None,
            "library_only": True, "job": job}


# --- HTTP -------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/stats":
                return self._json(stats())
            if path == "/api/config":
                return self._json({"navidrome": NAVIDROME_URL, "deemix": DEEMIX_URL})
            if path == "/api/playlists":
                return self._json(playlists())
            if path == "/api/playlist-counts":
                return self._json(playlist_counts())
            if path == "/api/queue":
                return self._json(queue())
            if path == "/api/errors":
                return self._json(deemix_errors())
            if path == "/api/playlist":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                if not name:
                    return self._json({"error": "nome mancante"}, 400)
                return self._json(playlist_status(name))
            if path == "/api/fs":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                return self._json(fs_list((q.get("path") or [""])[0]))
            if path == "/api/systemd":
                return self._json(systemd())
            if path == "/api/actions":
                return self._json(
                    {k: v["label"] for k, v in ACTIONS.items()}
                )
            if path == "/api/jobs":
                return self._json(job_list())
            if path.startswith("/api/jobs/"):
                jid = path.rsplit("/", 1)[1]
                with jobs_lock:
                    job = jobs.get(jid)
                return self._json(job) if job else self._json({"error": "?"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0))

        # File upload: the body is the mp3 itself, not JSON.
        if path == "/api/playlist/upload":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            filename = (q.get("filename") or [""])[0]
            if not name or not length:
                return self._json({"error": "dati mancanti"}, 400)
            if length > 60 * 1024 * 1024:
                return self._json({"error": "file troppo grande (max 60 MB)"}, 413)
            data = self.rfile.read(length)
            try:
                return self._json(add_track_file(name, filename, data))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # File-browser upload into a chosen library folder (audio, up to 200 MB).
        if path == "/api/fs/upload":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            rel = (q.get("path") or [""])[0]
            filename = (q.get("filename") or [""])[0]
            if not filename or not length:
                return self._json({"error": "dati mancanti"}, 400)
            if length > 200 * 1024 * 1024:
                return self._json({"error": "file troppo grande (max 200 MB)"}, 413)
            data = self.rfile.read(length)
            try:
                return self._json(fs_upload(rel, filename, data))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # Album import: one track + its tag fields, written for the user.
        if path == "/api/fs/import":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            meta = {k: (q.get(k) or [""])[0]
                    for k in ("album", "albumartist", "artist", "title", "track", "total")}
            filename = (q.get("filename") or [""])[0]
            if not filename or not length:
                return self._json({"error": "dati mancanti"}, 400)
            if length > 200 * 1024 * 1024:
                return self._json({"error": "file troppo grande (max 200 MB)"}, 413)
            data = self.rfile.read(length)
            try:
                return self._json(fs_import(meta, filename, data))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        try:
            if path == "/api/run":
                act = ACTIONS.get(body.get("action"))
                if not act:
                    return self._json({"error": "azione sconosciuta"}, 400)
                return self._json({"job": run_job(act["label"], act["argv"])})
            if path == "/api/detach":
                name = body.get("name")
                if not name:
                    return self._json({"error": "nome mancante"}, 400)
                return self._json({"job": detach(name, bool(body.get("drop")))})
            if path == "/api/queue/clean":
                uuid = body.get("uuid")
                # Only real queue files count; queue-clean re-validates too.
                if uuid not in {q["uuid"] for q in queue()}:
                    return self._json({"error": "uuid sconosciuto"}, 400)
                return self._json({"job": clean_queue(uuid)})
            if path == "/api/playlist/ignore":
                name, key = body.get("name"), body.get("key")
                if not name or not key:
                    return self._json({"error": "dati mancanti"}, 400)
                return self._json(playlist_ignore(name, key, bool(body.get("on"))))
            if path == "/api/fs/mkdir":
                return self._json(fs_mkdir(body.get("path", ""), body.get("name", "")))
            if path == "/api/scan":
                return self._json({"job": scan_library()})
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dashboard su http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
