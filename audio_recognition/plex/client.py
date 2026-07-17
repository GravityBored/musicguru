"""Resolve a (artist, title) pair to a real, streamable Plex part key.

The old code built this:

    {PLEX_BASE_URL}/music/{Sanitized_Artist}-{Sanitized_Title}.mp3?X-Plex-Token=...

Plex has never served media at that path. Every line of every generated
playlist was a 404. Real streaming requires searching the library for the
track, then using the part key Plex hands back.
"""
import logging
import re
import unicodedata

import requests

from ..config import PLEX_BASE_URL, PLEX_MUSIC_TYPE, PLEX_TIMEOUT, PLEX_TOKEN

log = logging.getLogger("audio_recognition.plex")

_cache: dict[tuple[str, str], dict | None] = {}


def configured() -> bool:
    return bool(PLEX_BASE_URL and PLEX_TOKEN)


def _headers() -> dict:
    return {"Accept": "application/json", "X-Plex-Token": PLEX_TOKEN}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)          # drop "(Remastered 2011)" etc.
    s = re.sub(r"[^0-9a-z]+", "", s.lower())
    return s


def _query_title(title: str) -> str:
    """A search string Plex will actually match: parentheticals dropped, trimmed."""
    return re.sub(r"\(.*?\)|\[.*?\]", "", title or "").strip()


def _lookup(artist: str, title: str) -> dict | None:
    """Best library candidate for (artist, title), or None. The returned dict has
    rating_key / part_key (part_key may be None when the item has no streamable
    part) / duration / title / artist. Cached per normalized (artist, title)."""
    if not configured():
        return None

    ck = (_norm(artist), _norm(title))
    if ck in _cache:
        return _cache[ck]

    want_artist, want_title = ck
    if not want_title:
        _cache[ck] = None
        return None

    try:
        resp = requests.get(
            f"{PLEX_BASE_URL}/search",
            # Search by TITLE ONLY. Plex track search matches the query against
            # the track title, so folding the artist into the query ("Pink Floyd
            # Signs of Life") makes tracks that ARE in the library return
            # nothing -- the classic false "not in Plex". We filter the
            # candidates by artist ourselves, below.
            params={"query": _query_title(title) or title,
                    "type": PLEX_MUSIC_TYPE, "limit": 50},
            headers=_headers(),
            timeout=PLEX_TIMEOUT,
        )
        resp.raise_for_status()
        items = (resp.json().get("MediaContainer") or {}).get("Metadata") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("Plex search failed for %s - %s: %s", artist, title, e)
        return None  # transient failure: deliberately not cached

    best = None       # a match that also has a streamable part
    best_any = None   # a metadata match (existence), part or not
    for item in items:
        item_title = _norm(item.get("title", ""))
        item_artist = _norm(item.get("grandparentTitle", ""))
        if not item_title:
            continue
        # Titles/artists rarely match char-for-char (remaster suffixes, "feat.",
        # punctuation), so compare on the normalized forms and allow either to
        # contain the other. Artist must corroborate, which keeps it honest.
        title_ok = (item_title == want_title
                    or want_title in item_title or item_title in want_title)
        artist_ok = (not want_artist
                     or want_artist in item_artist or item_artist in want_artist)
        if not (title_ok and artist_ok):
            continue
        exact = item_title == want_title and item_artist == want_artist
        media = (item.get("Media") or [{}])[0]
        part = (media.get("Part") or [{}])[0]
        cand = {
            "rating_key": item.get("ratingKey"),
            "part_key": part.get("key"),
            "duration": int((item.get("duration") or 0) / 1000) or None,
            "title": item.get("title"),
            "artist": item.get("grandparentTitle"),
        }
        if best_any is None or exact:
            best_any = cand
        if cand["part_key"] and (best is None or exact):
            best = cand
        if exact and cand["part_key"]:
            break

    result = best or best_any
    if result is None:
        log.info("No Plex match for %s - %s", artist, title)
    _cache[ck] = result
    return result


def find_track(artist: str, title: str) -> dict | None:
    """A streamable match (guaranteed part_key) -- for streaming and M3U export."""
    m = _lookup(artist, title)
    return m if m and m.get("part_key") else None


def in_library(artist: str, title: str) -> bool:
    """Whether the library has this track at all, streamable or not. Used by the
    want-list and the per-row 'in Plex' badge -- existence, not playability."""
    return _lookup(artist, title) is not None


def open_stream(part_key: str, range_header: str | None = None) -> requests.Response:
    """Streaming GET against Plex, honoring the client's Range header."""
    headers = {"X-Plex-Token": PLEX_TOKEN}
    if range_header:
        headers["Range"] = range_header
    return requests.get(
        f"{PLEX_BASE_URL}{part_key}",
        headers=headers,
        stream=True,
        timeout=PLEX_TIMEOUT,
    )


_machine_id: str | None = None


def _machine() -> str | None:
    """The server's machineIdentifier, needed to build a playlist item URI."""
    global _machine_id
    if _machine_id or not configured():
        return _machine_id
    try:
        r = requests.get(f"{PLEX_BASE_URL}/identity", headers=_headers(), timeout=PLEX_TIMEOUT)
        r.raise_for_status()
        _machine_id = (r.json().get("MediaContainer") or {}).get("machineIdentifier")
    except (requests.RequestException, ValueError) as e:
        log.warning("Plex identity failed: %s", e)
    return _machine_id


def _find_playlist(title: str) -> str | None:
    try:
        r = requests.get(
            f"{PLEX_BASE_URL}/playlists",
            headers=_headers(),
            params={"playlistType": "audio"},
            timeout=PLEX_TIMEOUT,
        )
        r.raise_for_status()
        for pl in (r.json().get("MediaContainer") or {}).get("Metadata") or []:
            if (pl.get("title") or "").strip().lower() == title.strip().lower():
                return pl.get("ratingKey")
    except (requests.RequestException, ValueError) as e:
        log.warning("Plex playlist list failed: %s", e)
    return None


def create_or_append_playlist(title: str, rating_keys: list[str]) -> dict:
    """Create an audio playlist from these rating keys, or append to an existing
    one with the same title. Returns {'created': bool, 'playlist_key': str|None}."""
    machine = _machine()
    if not machine:
        raise RuntimeError("no Plex machine identifier")
    uri = (f"server://{machine}/com.plexapp.plugins.library/library/metadata/"
           + ",".join(str(k) for k in rating_keys))

    existing = _find_playlist(title)
    if existing:
        r = requests.put(
            f"{PLEX_BASE_URL}/playlists/{existing}/items",
            headers=_headers(), params={"uri": uri}, timeout=PLEX_TIMEOUT,
        )
        r.raise_for_status()
        return {"created": False, "playlist_key": existing}

    r = requests.post(
        f"{PLEX_BASE_URL}/playlists",
        headers=_headers(),
        params={"type": "audio", "title": title, "smart": 0, "uri": uri},
        timeout=PLEX_TIMEOUT,
    )
    r.raise_for_status()
    key = None
    try:
        key = ((r.json().get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("ratingKey")
    except ValueError:
        pass
    return {"created": True, "playlist_key": key}
