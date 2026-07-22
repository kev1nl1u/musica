# music-stack

Self-hosted music streaming on an Oracle Cloud Always Free ARM64 box: download
from Deezer, serve over the Subsonic API, listen on Android.

Draft / demo. The compose file is unremarkable — the interesting part is the
four scripts, and the behaviours they exist to work around. Most of them are not
documented by either project and took a while to pin down, so they are written
up here in full.

```
Deezer ──► deemix ──► music/ ──► Navidrome ──► Symfonium
                        ▲            ▲
                   scripts/     Caddy + Google OAuth
```

| Piece | Role |
|---|---|
| [Navidrome](https://www.navidrome.org/) | library and Subsonic API, port 4533 |
| [deemix](https://github.com/bambanah/deemix) | downloader, bound to loopback |
| [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) | one Google login for every private app |
| Caddy | TLS and reverse proxy, installed on the host |

## Auth

`auth.example.com` is the only redirect URI registered with Google. Protected
sites delegate to it with Caddy's `forward_auth`, so adding an app is a
Caddyfile block and nothing else — see `caddy/auth.example.com.caddy`.

The session cookie is scoped to the parent domain so it spans subdomains. That
is what makes single sign-on work, and it also means any subdomain can read the
cookie: fine for a personal deployment, not for shared hosting.

oauth2-proxy sees requests coming from the Docker bridge gateway, not from
`127.0.0.1` — `--trusted-proxy-ip` has to name the bridge address or the proxy
refuses to trust the forwarded headers.

## Library layout

Everything lands in `Artist/Album/NN - Title.mp3`. Navidrome groups by tags, not
folders, so the layout is only for humans — but a flat library makes filename
collisions possible, and with `overwriteFile: 'n'` a collision means the second
track is silently skipped rather than overwritten.

## The scripts

### `playlist-namer.py`

deemix writes every playlist as the literal `%playlist%.m3u8` — it never
substitutes the template. With a download queue each finished playlist
overwrites the last, so the file has to be moved out of the library the moment
it is closed, then identified afterwards.

Identification matches the file's track list against the Deezer API rather than
reading the most recent id out of deemix's log: with several downloads
interleaved, the last id in the log is not necessarily the one that produced the
file. A file that matches nothing above 50% is left in a holding directory
rather than guessed at.

Runs as a systemd service watching the library with inotify.

### `album-remap.py`

A Deezer playlist may reference the album release of a track or its standalone
single. deemix tags whatever the playlist points at, so a track from an album
arrives tagged as a one-track album of its own — and clients group by tag. An
11-track album can show up as nine separate entries plus the two tracks that
happened to come from the album release.

This asks Deezer whether an album or EP by the same artist contains the track,
then rewrites the tags and moves the file. Audio data is never touched.

Four things that are easy to get wrong, each of which produced a wrong answer
before it was found:

- **`/search?q=track:"..."` returns one release per recording, and picks the
  single.** The album version never appears in the results, so searching by
  title cannot discover album membership. The lookup has to run the other way:
  artist → releases → tracklists.
- **Navidrome keys an album on _(name, album artist)_.** Fixing `TALB` alone
  splits one album into an entry per guest line-up, because a single release
  puts that track's collaborators in `TPE2`. The album artist has to be
  normalised too; track artists stay in `TPE1`.
- **deemix joins multiple artists with `/` in `TPE1`.** Grouping by the raw tag
  invents artists like `Martin Garrix/ZEDD` that Deezer has never heard of, and
  every collaboration silently fails to match.
- **Deezer marks DJ compilations as `record_type: album`.** They are filtered by
  counting distinct artists across the tracklist, which also rejects genuine
  albums carrying many featured artists — a deliberate trade, since a wrong
  album is worse than a missed one. Known bad pairs live in
  `scripts/album-remap-exclude.txt`.

An album that exists only off Deezer — an Apple-exclusive live set, say — is the
opposite problem: its tracks would be matched into the artist's studio releases
and scattered. Such albums are **protected**. A bare album name (no `->`) in the
exclusions file, or an entry in `cache/protected-albums.json`, makes the remap
skip every track tagged with it. The dashboard's album import writes that file
itself, so anything added by hand is protected without a separate step.

### `playlist-repair.py`

deemix writes an m3u8 once, when the job finishes, listing only what downloaded
successfully. A track that failed at the time and was fetched by hand later
stays orphaned — nothing ever adds it.

Rather than patch the file, it is regenerated: order from Deezer, paths from
whatever the library actually holds. That also absorbs any file moves, and
reports what is still genuinely missing. A playlist is matched to its m3u8 by
title, with ties broken by comparing track lists, since an account can hold
several playlists sharing a name.

The account's playlists are readable through the public API without a token.

Matching is on the exact name first, not the normalised one: several of these
playlists are named only with symbols or emoji (`🔊`, `✚✖︎`, `.`), which all
normalise to the empty string and would otherwise resolve to each other.


### Excluding a track

A playlist can carry a track that will never be in the library -- unavailable on
Deezer, or simply unwanted. Left alone it is reported missing forever. From a
playlist's detail page (click its name in the dashboard) a missing track can be
**excluded**: `playlist-repair.py` then drops it from the playlist's expected
set, so it counts neither as present nor missing, and a playlist whose only gap
was that track reads as complete.

Missing tracks can also be filled from the browser: an **Aggiungi file** button
takes a drag-and-dropped mp3, matches it by tags to a track some playlist is
missing, retags it to match exactly, files it in the library and runs repair so
it joins immediately. A file that matches nothing is not rejected -- it is filed
in the library under its own tags. Adding a song no playlist happens to want is
fine.

Exclusions live in `cache/playlist-ignore.json`, keyed by Deezer playlist id.
Manage them from the CLI too:

```sh
playlist-repair.py --status "Name"          # tracks as JSON, with state
playlist-repair.py --ignore "Name" KEY      # KEY is the track's normalised key
playlist-repair.py --unignore "Name" KEY
```

### `playlist-detach.py`

A playlist imported from a file is marked `sync=1`, and Navidrome re-imports it
from that file **on every full scan — even when the file has not changed**.
Incremental scans leave playlists alone; full scans do not. So an edit made from
a client survives only until the next full scan, and the scripts here trigger
one whenever they change something.

Detaching moves the m3u8 out of the library and clears the sync flag and path,
leaving a playlist indistinguishable from one created by hand. Removing the file
is enough to stop the re-import on its own — Navidrome keeps a playlist whose
file has vanished — but clearing the flag makes the state honest.

Deliberately manual, and deliberately one-way: afterwards nothing tops the
playlist up, which is the point. Reversible in practice — the m3u8 is archived
to `deemix/playlists-detached/`, not deleted.

Needs `sudo`: Navidrome runs as root in its container, so the database it
creates is root-owned. It stops Navidrome for the update, because the server
caches playlists in memory and would not otherwise see the change.

## Playlist lifecycle

```
download from Deezer
   ↓   playlist-namer    gives the m3u8 its real name
   ↓   album-remap       fixes tags and folders
   ↓   playlist-repair   picks up late arrivals
   ↓   playlist-detach   cuts it loose  ← manual
playlist is yours: editable from any client, nothing overwrites it
```

While a playlist is still synced, Deezer is the source of truth. Whether an
addition needs a detach depends on where the track is, not on how it is added:

| | Detach needed? |
|---|---|
| Track is in the Deezer playlist but was never downloaded | **No** — drop the file in the library and `playlist-repair` inserts it, in the right position |
| Track is not in the Deezer playlist at all | **Yes** |
| Reordering or removing tracks | **Yes** |

Before detaching, do not add tracks from the client: the change lands only in
the database and is erased by the next full scan. Add the *file* to the library
and let `playlist-repair` write it into the m3u8.

After detaching it inverts — client edits stick, and nothing arrives from Deezer
any more.

Re-downloading a detached playlist creates a *second* playlist with the same
name, one curated and one synced. Nothing is lost, but they have to be merged by
hand.


## Dashboard

A control panel at `musica.example.com`, behind the same Google login as
everything else. `scripts/dashboard.py` is a standard-library HTTP server -- no
framework to install -- serving one page plus a small JSON API.

What it shows: library stats, every playlist with its downloaded-vs-total count
and synced/detached state, the deemix queue with each job's failed tracks, a
hung-queue warning, and the timer's health. What it does: runs album-remap and
playlist-repair as background jobs whose output streams into the page, clears a
stuck queue job (via `queue-clean.py`), and -- the reason it exists -- detaches
a playlist from a button instead of the CLI.

Clicking a playlist name opens a detail page listing its full Deezer tracklist,
each track marked present, missing or excluded, with a button to exclude a
missing track (or restore it) -- the exclusion flow above, from the browser.
Each panel has an ⓘ toggle explaining what it shows and the caveats.

The queue and error panels **self-heal against the library**: deemix never
rewrites `errors.txt` or a job's failure list once a track is fetched by hand,
so a line whose track is now present is hidden rather than reported as a
still-open failure.

Every mutating call is an allowlisted action, never a free-form command line, so
the web layer cannot run anything the scripts do not already expose.

### Adding music by hand

Not everything comes from Deezer, so the dashboard can put files into the
library directly:

- **Aggiungi file** — one file, matched to a missing playlist track or, failing
  that, filed in the library on its own (see *Excluding a track* above).
- **Cartelle** — a small file browser scoped strictly to `music/`: walk folders,
  make new ones, upload audio into a chosen one. Every path is resolved and
  refused if it escapes `music/`.
- **Importa album** — for a whole album that has to be filed by hand (an
  Apple-exclusive live set, say). Album name and album artist come once from a
  form; per-track title and number are guessed from each filename and can be
  edited. Tags are written through mutagen's format-agnostic interface so mp3,
  m4a and flac all get the fields Navidrome groups on, and the album is
  auto-protected from album-remap.

Uploads run in the background with real progress in a tray, so a large import
does not block the page; leaving while one is still running is guarded by a
confirm. Each batch triggers a single Navidrome rescan when it finishes.

Only audio extensions are accepted, and the whole thing sits behind oauth2-proxy
like everything else, so filing arbitrary paths is not a concern a stranger
could reach.

Detach still shells out to `playlist-detach.py` through sudo, so the database
write stays in one audited place. Note the server already grants this user
passwordless sudo, so the narrow sudoers rule documents intent rather than
confining anything; the real boundary is oauth2-proxy in front.

## Scheduling

`album-remap.timer` runs the remap and the repair every 15 minutes. Both refuse
to act while the library is still being written to: deemix writes a playlist's
m3u8 only after every track in the job is done, so moving files mid-queue would
leave that m3u8 pointing at paths that no longer exist. A quiet period is a
simpler guard than trying to track the queue.

Navidrome is only asked for a full scan when something actually changed. Moved
files leave their old paths in the database and only a full scan purges them
(`ND_SCANNER_PURGEMISSING: full`) — `always` would be a trap, since a music
volume that failed to mount would look like an empty library and take every play
count with it.

Moving a file does **not** change its `media_file.id`, so playlists that
reference it by id survive the remap.

## deemix settings worth knowing

Defaults and migrations that cost time here:

- **`tags.useNullSeparator` does nothing.** It appears in the settings schema,
  the UI checkbox and twenty translations, but no code reads it. The separator
  is decided by `multiArtistSeparator`.
- **`createSingleFolder` does two things.** Besides creating the album folder for
  a single-track download, it selects the filename template: with it off, single
  tracks silently fall back to `tracknameTemplate` and lose their track number.
- **Keep `playlistTracknameTemplate` and `albumTracknameTemplate` identical.**
  A playlist download uses the former, an album download the latter. If they
  differ, the same track arriving by different routes lands in the same folder
  under two names, and `overwriteFile: 'n'` cannot see the duplicate.
- **`maxBitrate` regressed on the v3 → v4 migration.** The old string `'3'` was
  not recognised as a number and fell back to `1` — MP3 128 — with no warning.
- **A permanently unavailable track can hang the queue forever.** With
  `fallbackSearch` and `fallbackBitrate` on, deemix loops: no alternative id →
  bitrate unavailable → lower it → still unavailable → repeat, with no attempt
  limit. One such job ran for two days, held a `queueConcurrency` slot and grew
  to 2 GB of RSS. It is not visible in the UI once it drops out of `order.json`,
  but its file stays in `config/queue/` and is reloaded on every restart.
  Symptom: `docker logs deemix | grep -c "Track not available"` climbing.
  Fix: stop the container and delete the offending `config/queue/*.json`.

## Setup

```sh
cp oauth2-proxy/.env.example oauth2-proxy/.env   # fill in Google credentials
echo you@example.com > oauth2-proxy/authenticated-emails.txt
cp config.env.example config.env                 # Deezer id + app URLs
docker compose up -d

sudo apt install python3-mutagen
sudo cp scripts/*.service scripts/*.timer /etc/systemd/system/
sudo install -m 440 scripts/music-dashboard.sudoers /etc/sudoers.d/music-dashboard
sudo systemctl enable --now deemix-playlist-namer.service album-remap.timer \
    music-dashboard.service
```

Then append `caddy/musica.example.com.caddy` to the Caddyfile and add the DNS
record; the dashboard binds to `127.0.0.1:8765` until it is proxied.

`album-remap.py` and `playlist-repair.py` are dry-run by default; pass `--apply`
to write. `playlist-detach.py` and `queue-clean.py` with no arguments list what
they would act on.

Deployment-specific values live in `config.env` (gitignored) — the Deezer user
id whose playlists are reconciled, and the app URLs for the dashboard's header
links. Copy `config.env.example` and fill it in. The tree root is derived from
the scripts' own location, so nothing else is path-specific.
