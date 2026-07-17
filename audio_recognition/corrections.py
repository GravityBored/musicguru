"""Learned corrections for Shazam misrecognitions.

A confirmed correction maps the RAW (artist, title) Shazam returned to the
canonical spelling. It is applied to every future recognition in the pipeline
*and* back-applied to existing rows when added -- so one manual fix cleans up
the past and stops the same miss from ever being logged wrong again.

Loaded once at startup and held in memory. Flask and the capture pipeline run
in one process, so a correction added through the web UI is visible to the
pipeline immediately via the shared, lock-guarded dict here.
"""
import logging
import re
import threading
import unicodedata

from .storage import db

log = logging.getLogger("audio_recognition.corrections")

_PARENS = re.compile(r"\(.*?\)|\[.*?\]")
_NON = re.compile(r"[^0-9a-z]+")

_lock = threading.Lock()
_map: dict[str, tuple[str, str]] = {}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NON.sub("", _PARENS.sub("", s.lower()))


def raw_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}\u0000{_norm(title)}"


def load() -> None:
    """Populate the in-memory map from the DB. Safe to call again to refresh."""
    global _map
    m = db.load_corrections()
    with _lock:
        _map = m
    log.info("Loaded %d correction(s).", len(m))


def apply(artist: str, title: str) -> tuple[str, str]:
    """Return the canonical (artist, title) if this raw pair has a correction,
    otherwise the input unchanged."""
    with _lock:
        hit = _map.get(raw_key(artist, title))
    return hit if hit else (artist, title)


def add(raw_artist: str, raw_title: str, artist: str, title: str) -> int:
    """Persist and memoize a correction, then relabel existing plays that match
    the raw pair. Returns the number of historical rows relabeled."""
    key = raw_key(raw_artist, raw_title)
    db.save_correction(key, artist, title)
    with _lock:
        _map[key] = (artist, title)
    return db.relabel(raw_title, raw_artist, title, artist)
