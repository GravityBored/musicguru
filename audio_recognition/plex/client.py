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


def find_track(artist: str, title: str) -> dict | None:
    """Return {'part_key', 'duration', 'rating_key', 'title', 'artist'} or None."""
    if not configured():
        return None

    ck = (_norm(artist), _norm(title))
    if ck in _cache:
        return _cache[ck]

    try:
        resp = requests.get(
            f"{PLEX_BASE_URL}/search",
            params={"query": f"{artist} {title}".strip(), "type": PLEX_MUSIC_TYPE},
            headers=_headers(),
            timeout=PLEX_TIMEOUT,
        )
        resp.raise_for_status()
        items = (resp.json().get("MediaContainer") or {}).get("Metadata") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("Plex search failed for %s - %s: %s", artist, title, e)
        return None  # deliberately not cached: transient failure

    want_artist, want_title = ck
    best = None
    for item in items:
        item_title = _norm(item.get("title", ""))
        item_artist = _norm(item.get("grandparentTitle", ""))
        if not item_title:
            continue
        exact = item_title == want_title and item_artist == want_artist
        loose = want_title and want_title in item_title and (
            not want_artist or want_artist in item_artist
        )
        if exact or (loose and best is None):
            media = (item.get("Media") or [{}])[0]
            part = (media.get("Part") or [{}])[0]
            if not part.get("key"):
                continue
            best = {
                "rating_key": item.get("ratingKey"),
                "part_key": part["key"],
                "duration": int((item.get("duration") or 0) / 1000) or None,
                "title": item.get("title"),
                "artist": item.get("grandparentTitle"),
            }
            if exact:
                break

    if best is None:
        log.info("No Plex match for %s - %s", artist, title)
    _cache[ck] = best
    return best


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
