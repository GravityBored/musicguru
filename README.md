# musicguru

Continuous line-in music recognition, logging, and browsing — built to sit on a
Raspberry Pi wired to a stereo's tape-out and quietly keep a searchable history
of everything that plays.

Every few seconds it records a short segment from a line-in device, identifies
the music, and logs it to MySQL. It drives an optional small framebuffer display
(album art via `feh`) and serves a web console for browsing history, statistics,
want-lists, and Plex playlists.

## Highlights

- **Recognition with a local cache.** Segments are identified by Shazam, but
  every identification is fingerprinted with Chromaprint and stored, so repeats
  are recognized locally without a network round-trip. An EMA vote keyed on a
  normalized identity locks the track and ignores spelling/remaster variants.
- **Durable cover art.** Covers are re-encoded and stored in the database (and a
  disk cache), so art survives lost caches and is never re-fetched from the
  internet after it's first seen.
- **Web console.** Now-playing panel, full searchable archive with streaming
  deep-links, want-list vs. your Plex library, and a rich stats page
  (listening clock, streaks, sessions, per-day calendar heatmap). One-click
  correction of misheard tracks that also fixes past plays and future ones.
- **Optional integrations.** Plex playlists + streaming proxy, Last.fm
  scrobbling, Home Assistant now-playing (webhook/MQTT), silence watchdog
  alerts, and a Prometheus `/metrics` endpoint. Each is inert until configured.
- **Login.** Optional username/password login (signed-cookie sessions) for the
  console, with a token path for machine callers.
- **Resilient.** Failed DB writes are spooled to disk and replayed in order;
  schema migrations run automatically at startup.

A complete feature/reference guide is served in-app at **`/docs`**.

## Requirements

**System packages**

- `alsa-utils` — `arecord` capture
- `ffmpeg` — audio decoding for `pydub`
- `libchromaprint-tools` — `fpcalc`, for local recognition (optional; without
  it, everything still works via Shazam)
- `feh` + a TrueType font — for the physical display (optional)
- MySQL or MariaDB

**Python**

```bash
pip install -r requirements.txt
```

## Configuration

Everything is set through `AR_*` environment variables. The only strictly
required one is `AR_DB_PASSWORD`. Copy `.env.example` and fill in what you need;
load it before launch (systemd `EnvironmentFile=` or `python-dotenv`). The full
annotated list lives in [`audio_recognition/config.py`](audio_recognition/config.py)
and in the in-app docs.

To require a login on the console:

```bash
# generate a password hash
python -m audio_recognition.webapp.auth 'your password'
# then set (e.g. in your .env)
export AR_WEB_USER=admin
export AR_WEB_PASSWORD_HASH='scrypt:...'   # the printed hash
```

## Run

```bash
python -m audio_recognition        # recognizer + web console
# console at http://127.0.0.1:8000/
```

The console binds to loopback by default. To expose it on a LAN, put a reverse
proxy in front and set a login (above) — or `AR_WEB_TOKEN` for token-only
access.

## Database

Create the database and a user, grant privileges, and set `AR_DB_PASSWORD`. All
tables (`recognized_songs`, `corrections`, `segment_counts`, `known_tracks`,
`fingerprints`, `cover_blobs`) and columns are created and migrated
automatically on first run.

## Tests

Logic-level offline tests (no live DB/Shazam/network required):

```bash
python tests/test_offline.py
```

## License

MIT — see [LICENSE](LICENSE).
