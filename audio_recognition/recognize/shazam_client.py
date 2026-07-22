import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass

from shazamio import Shazam

from .. import config

log = logging.getLogger("audio_recognition.recognize")

_PARENS = re.compile(r"\(.*?\)|\[.*?\]")
_NONALNUM = re.compile(r"[^0-9a-z]+")


def _key_norm(s: str) -> str:
    """Fold accents, drop parentheticals/punctuation, lowercase -- so
    'Song (2011 Remaster)' and 'Song' collapse to one vote key."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NONALNUM.sub("", _PARENS.sub("", s.lower()))

_client: Shazam | None = None


def _get_client() -> Shazam:
    global _client
    if _client is None:
        _client = Shazam()
    return _client


@dataclass
class Track:
    key: str | None
    title: str
    artist: str
    album: str | None
    genre: str | None
    duration: int | None  # seconds
    cover_url: str | None

    @property
    def ident(self) -> str:
        return f"{self.title} \u2013 {self.artist}"

    @property
    def match_key(self) -> str:
        """Key the EMA vote on a normalized identity so a remaster, a live take,
        and a 'feat.' spelling variant don't split the vote and stall the lock.
        Display still uses the raw title/artist off this Track."""
        return f"{_key_norm(self.title)}\u0000{_key_norm(self.artist)}"


def _section_metadata(raw: dict) -> dict:
    """Flatten sections[type=SONG].metadata (a list of {title,text}) into a dict."""
    out = {}
    for section in raw.get("sections") or []:
        if section.get("type") == "SONG":
            for item in section.get("metadata") or []:
                title = item.get("title")
                if title:
                    out[title] = item.get("text")
    return out


def parse_track(raw: dict) -> Track:
    """Pull the fields the DB actually has columns for.

    The old pipeline passed None for album and genre and called
    raw.get('duration'), which never exists at the top level of a recognize()
    payload. Result: three permanently NULL columns, a blank Album/Genre in the
    UI, a trivia lookup for album=undefined, and '#EXTINF:None' in every M3U.
    """
    meta = _section_metadata(raw)
    genres = raw.get("genres") or {}

    duration = None
    for candidate in (meta.get("Duration"), raw.get("duration")):
        if candidate is None:
            continue
        try:
            if isinstance(candidate, str) and ":" in candidate:  # "3:47"
                mins, _, secs = candidate.partition(":")
                duration = int(mins) * 60 + int(secs)
            else:
                duration = int(candidate)
            break
        except (TypeError, ValueError):
            continue

    return Track(
        key=raw.get("key"),
        title=raw.get("title") or "Unknown",
        artist=raw.get("subtitle") or "Unknown",
        album=meta.get("Album"),
        genre=genres.get("primary"),
        duration=duration,
        cover_url=(raw.get("images") or {}).get("coverart"),
    )


async def recognize(audio_path: str) -> Track | None:
    """Run Shazam recognition on the given file. Returns None if unrecognized."""
    try:
        res = await asyncio.wait_for(_get_client().recognize(audio_path), timeout=config.SHAZAM_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("Shazam timed out after %.1fs", config.SHAZAM_TIMEOUT)
        return None
    except Exception as e:
        log.warning("Shazam error: %s", e)
        return None

    raw = (res or {}).get("track")
    if not raw:
        log.debug("No match")
        return None

    track = parse_track(raw)
    log.info(
        "Shazam: %s - %s [album=%s genre=%s dur=%s]",
        track.title, track.artist, track.album, track.genre, track.duration,
    )
    return track
