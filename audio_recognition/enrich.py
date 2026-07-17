"""Metadata enrichment for freshly recognized tracks.

Shazam's recognize() payload almost never carries a track duration and often
omits album and genre. That left three columns permanently NULL, blanked
Album/Genre in the UI, put "#EXTINF:None" in every M3U -- and, because the web
player's progress bar is (elapsed / duration), pinned the Now Playing timer at
0% forever. There is no duration to divide by, so the bar never moves.

This module fills those gaps from Last.fm (the same source backfill_metadata.py
already uses for historical rows), with a MusicBrainz fallback used *only* for
duration, which Last.fm reports inconsistently. Doing it here means the live
pipeline and the batch backfill share one resolver instead of drifting apart.

Everything here is synchronous requests + sleeps. Call enrich_track() from a
worker thread (asyncio.to_thread) so it never blocks the capture loop.

Note on the timer: enrichment gives a real *duration*, so the bar fills and the
"m:ss / m:ss" readout is correct. It cannot give a play *offset* -- elapsed is
measured from when the track was first recognized, not from the true position
in the song. Tune in mid-song and the bar starts at 0. Getting real offset
needs a broadcast-monitoring recognizer (ACRCloud/AudD); it is out of scope for
the keep-Shazam path.
"""
import logging
import re
import threading
import time
import unicodedata

import requests

from . import config

log = logging.getLogger("audio_recognition.enrich")

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"

# Last.fm tags are free-text user tags, so the top one is frequently not a genre
# at all. Anything matching this is skipped in favour of the next tag down.
# (Identical to the backfill list -- kept here so both callers share one copy.)
TAG_BLOCKLIST = re.compile(
    r"^(seen live|favorit|favourite|awesome|best|love|beautiful|amazing|cool|"
    r"my |albums i own|under \d+ listeners|\d{2,4}s?$|male vocalist|female vocalist|"
    r"usa$|uk$|american$|british$|english$|spotify|good$|great$|epic$|"
    r"masterpiece|classic$|music$|band$|check out|to check|listen)",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """Fold accents/punctuation/parentheticals for tolerant artist matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def _pick_genre(tags) -> str | None:
    """First usable tag from a Last.fm toptags/tags block, or None."""
    if isinstance(tags, dict):
        tags = tags.get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    for t in tags or []:
        name = (t.get("name") or "").strip() if isinstance(t, dict) else str(t).strip()
        if not name or len(name) > 40 or TAG_BLOCKLIST.match(name):
            continue
        return name.title()
    return None


# --- Last.fm -------------------------------------------------------------

def _lastfm(session: requests.Session, method: str, **params) -> dict | None:
    """One Last.fm call. Returns parsed JSON, or None on any error/not-found."""
    if not config.LASTFM_API_KEY:
        return None
    params.update({"method": method, "api_key": config.LASTFM_API_KEY, "format": "json"})
    try:
        r = session.get(LASTFM_URL, params=params, timeout=config.LASTFM_TIMEOUT)
    except requests.RequestException as e:
        log.debug("%s request failed: %s", method, e)
        return None

    if r.status_code == 429:
        log.warning("Rate limited by Last.fm; backing off 10s")
        time.sleep(10)
        return None
    try:
        data = r.json()
    except ValueError:
        log.debug("%s returned non-JSON (status %s)", method, r.status_code)
        return None
    if "error" in data:
        # 6 = "track not found", which is routine and not worth shouting about.
        if data.get("error") != 6:
            log.debug("Last.fm error %s: %s", data.get("error"), data.get("message"))
        return None
    return data


def parse_lastfm_track(track: dict, want_artist: str) -> dict | None:
    """Pure parse of a track.getInfo 'track' object into album/genre/duration.

    autocorrect=1 will happily redirect to a different artist, so the returned
    artist is verified against what we asked for and rejected on mismatch.
    """
    if not track:
        return None

    got_artist = ((track.get("artist") or {}).get("name")) or ""
    if _norm(got_artist) and _norm(want_artist) and _norm(got_artist) != _norm(want_artist):
        log.debug("Rejecting %s -> %s (artist mismatch)", want_artist, got_artist)
        return None

    album = ((track.get("album") or {}).get("title")) or None
    genre = _pick_genre(track.get("toptags"))

    duration = None
    try:
        ms = int(track.get("duration") or 0)
        duration = ms // 1000 or None  # Last.fm reports milliseconds
    except (TypeError, ValueError):
        pass

    return {"album": album, "genre": genre, "duration": duration}


def resolve_lastfm(session: requests.Session, artist: str, title: str) -> dict | None:
    """Full Last.fm resolve: track info, plus an album-tag genre fallback.

    Returns {'album', 'genre', 'duration'} with any subset populated, or None
    if nothing usable came back. This is exactly what backfill_metadata.py needs,
    so it imports this instead of keeping its own copy.
    """
    data = _lastfm(session, "track.getInfo", artist=artist, track=title, autocorrect=1)
    meta = parse_lastfm_track((data or {}).get("track") or {}, artist)
    if meta is None:
        return None

    # Track-level tags are often empty; the album's tags are a reasonable proxy.
    if meta["album"] and not meta["genre"]:
        adata = _lastfm(session, "album.getInfo", artist=artist, album=meta["album"],
                        autocorrect=1)
        meta["genre"] = _pick_genre(((adata or {}).get("album") or {}).get("tags"))

    if not (meta["album"] or meta["genre"] or meta["duration"]):
        return None
    return meta


# --- MusicBrainz (duration fallback only) --------------------------------

_MB_LOCK = threading.Lock()
_mb_last_call = 0.0
# Lucene special characters that would otherwise break the query string.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lucene_escape(s: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", s or "")


def _mb_throttle() -> None:
    """MusicBrainz asks for <=1 request/second from anonymous clients."""
    global _mb_last_call
    with _MB_LOCK:
        wait = 1.1 - (time.time() - _mb_last_call)
        if wait > 0:
            time.sleep(wait)
        _mb_last_call = time.time()


def parse_mb_recordings(data: dict, want_artist: str) -> int | None:
    """Best-scoring recording whose artist matches and that has a length.

    MusicBrainz 'length' is in milliseconds; returns whole seconds, or None.
    """
    best = None  # (score, seconds)
    for rec in (data or {}).get("recordings") or []:
        length = rec.get("length")
        if not length:
            continue
        credited = " ".join(
            (ac.get("name") or (ac.get("artist") or {}).get("name") or "")
            for ac in rec.get("artist-credit") or []
        )
        if _norm(want_artist) and _norm(credited) and _norm(want_artist) not in _norm(credited):
            continue
        try:
            score = int(rec.get("score") or 0)
            seconds = int(length) // 1000
        except (TypeError, ValueError):
            continue
        if seconds and (best is None or score > best[0]):
            best = (score, seconds)
    return best[1] if best else None


def resolve_musicbrainz_duration(session: requests.Session, artist: str, title: str) -> int | None:
    """Query MusicBrainz for a track duration in seconds, or None."""
    query = f'recording:"{_lucene_escape(title)}" AND artist:"{_lucene_escape(artist)}"'
    _mb_throttle()
    try:
        r = session.get(
            MUSICBRAINZ_URL,
            params={"query": query, "fmt": "json", "limit": 5},
            headers={"User-Agent": config.MUSICBRAINZ_USER_AGENT},
            timeout=config.MUSICBRAINZ_TIMEOUT,
        )
    except requests.RequestException as e:
        log.debug("MusicBrainz request failed: %s", e)
        return None
    if r.status_code == 503:
        log.debug("MusicBrainz throttled (503)")
        return None
    try:
        data = r.json()
    except ValueError:
        log.debug("MusicBrainz returned non-JSON (status %s)", r.status_code)
        return None
    return parse_mb_recordings(data, artist)


# --- live entry point ----------------------------------------------------

_CACHE_LOCK = threading.Lock()
_cache: dict[str, dict | None] = {}   # norm-key -> resolved meta (or None miss)
_CACHE_MAX = 4096
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers["User-Agent"] = "audio_recognition/1.0"
        _session = s
    return _session


def _cache_get(key: str):
    with _CACHE_LOCK:
        return _cache.get(key, _MISS) if key in _cache else _MISS


def _cache_put(key: str, value) -> None:
    with _CACHE_LOCK:
        if len(_cache) >= _CACHE_MAX:
            # Cheap eviction: drop an arbitrary existing entry. The working set
            # (songs in current rotation) is tiny; this only trims cold history.
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = value


_MISS = object()


def _resolve(artist: str, title: str) -> dict | None:
    """Last.fm for album/genre/duration, MusicBrainz to fill a missing duration."""
    session = _get_session()
    meta = resolve_lastfm(session, artist, title) or {"album": None, "genre": None, "duration": None}

    if not meta.get("duration") and config.ENRICH_MUSICBRAINZ:
        dur = resolve_musicbrainz_duration(session, artist, title)
        if dur:
            meta["duration"] = dur

    if not (meta.get("album") or meta.get("genre") or meta.get("duration")):
        return None
    return meta


def enrich_track(track) -> "object":
    """Fill album/genre/duration on a recognize.Track that Shazam left missing.

    Mutates and returns the same Track. Never raises into the caller; on any
    failure the track is returned unchanged. Results are cached by normalised
    artist+title, so a song on repeat is looked up once, not every segment.
    """
    if not config.ENRICH_ENABLED:
        return track
    if track.album and track.genre and track.duration:
        return track  # Shazam already gave us everything (rare, but skip the call)

    key = f"{_norm(track.artist)}\u0000{_norm(track.title)}"
    cached = _cache_get(key)
    if cached is _MISS:
        try:
            cached = _resolve(track.artist, track.title)
        except Exception as e:  # enrichment must never take down the loop
            log.warning("Enrichment failed for %s - %s: %s", track.artist, track.title, e)
            cached = None
        _cache_put(key, cached)

    if not cached:
        return track

    # Fill only what Shazam left empty; never overwrite a value it did provide.
    if track.album is None and cached.get("album"):
        track.album = cached["album"]
    if track.genre is None and cached.get("genre"):
        track.genre = cached["genre"]
    if track.duration is None and cached.get("duration"):
        track.duration = cached["duration"]

    log.info(
        "Enriched %s - %s [album=%s genre=%s dur=%s]",
        track.title, track.artist, track.album, track.genre, track.duration,
    )
    return track
