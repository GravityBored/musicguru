"""Append every newly heard track to a playlist on each enabled service
(Spotify, Tidal, Plex), reliably.

Design: hearing a track and adding it to a service are decoupled. On a new play
the track is *queued* per enabled service (a fast DB write that can't fail the
pipeline). A background worker then flushes the queue, retrying until each add
succeeds -- so a service that's briefly down, disconnected, or rate-limited no
longer silently drops tracks. Successful/handled tracks move to auto_playlist_log
(deduped, survives restarts); the queue only holds outstanding work.
"""
import logging

from . import config, logging_setup, textmatch
from .plex import client as plex
from .services import spotify, tidal
from .storage import db

log = logging.getLogger("audio_recognition.autoplaylist")


def _key(artist: str, title: str) -> str:
    return f"{textmatch.norm(artist)}|{textmatch.norm(title)}"


def _enabled_services() -> list[str]:
    """Services toggled on and configured -- used for QUEUEING (connection is
    checked later, at flush time, so a disconnect doesn't drop the track)."""
    out = []
    if config.AUTO_PLAYLIST_SPOTIFY and spotify.configured():
        out.append("spotify")
    if config.AUTO_PLAYLIST_TIDAL and tidal.configured():
        out.append("tidal")
    if config.AUTO_PLAYLIST_PLEX and plex.configured():
        out.append("plex")
    return out


def _service_ready(svc: str) -> bool:
    if svc == "spotify":
        return spotify.connected()
    if svc == "tidal":
        return tidal.connected()
    if svc == "plex":
        return plex.configured()
    return False


def targets() -> list[str]:
    """Enabled + currently-usable services, for status/UX."""
    return [s for s in _enabled_services() if _service_ready(s)]


def enabled() -> bool:
    return bool(_enabled_services())


def _add_one(service: str, name: str, artist: str, title: str, album: str = None) -> str:
    """Returns 'added', 'present' (already in the playlist), or 'absent' (not
    found on the service)."""
    if service == "spotify":
        return spotify.add_to_named_playlist(name, artist, title, album)
    if service == "tidal":
        return tidal.add_to_named_playlist(name, artist, title, album)
    if service == "plex":
        rk = plex.match_rating_key(artist, title, album)
        if not rk:
            return "absent"
        res = plex.create_or_append_playlist(name, [rk])
        return "added" if res.get("added", 1) else "present"
    return "absent"


def enqueue(artist: str, title: str, album: str = None, plays: int = None) -> None:
    """Queue a newly heard track for each enabled service (skips ones already
    handled). Cheap; the flush worker does the actual adding.

    A track is only queued once it has been heard AUTO_PLAYLIST_MIN_PLAYS times,
    so the playlist reflects what actually gets played rather than every one-off.
    """
    svcs = _enabled_services()
    if not svcs:
        return
    threshold = max(1, int(config.AUTO_PLAYLIST_MIN_PLAYS))
    if threshold > 1:
        n = plays if plays is not None else db.play_count(artist, title)
        if n < threshold:
            log.debug("Auto-playlist: %s - %s has %d play(s), needs %d",
                      artist, title, n, threshold)
            return
    key = _key(artist, title)
    ov = db.get_album_override(artist, title)
    al = ov["album"] if ov else album
    name = config.AUTO_PLAYLIST_NAME
    for svc in svcs:
        # Already sitting in the actual playlist on the service? Nothing to do.
        if _in_playlist(svc, name, key):
            continue
        # Known to be unavailable on this service (searched before, not found)?
        # Skip re-searching it every play. Removal-from-playlist is handled by the
        # membership check above; this log is only about catalogue availability.
        if db.autoplaylist_seen(svc, key):
            continue
        db.autoplaylist_enqueue(svc, key, artist, title, al)


import threading
import time

_flush_lock = threading.Lock()
_backoff_until: dict[str, float] = {}   # service -> epoch seconds to resume
_consec_fail: dict[str, int] = {}       # service -> consecutive-error count
_PEAK = {"n": 0}                        # high-water queue depth, for the progress bar
_last_backfill: dict = {}               # breakdown from the last Sync all heard

# Live playlist membership, read from each service and cached briefly. This is
# the source of truth for "is the track already in the playlist" -- NOT our own
# log. So if you delete a track from the playlist, it becomes eligible again.
_members: dict[str, tuple] = {}         # service -> (set_of_keys, fetched_epoch)
_MEMBER_TTL = 300                       # seconds before re-reading the playlist


def _read_membership(svc: str, name: str) -> set:
    if svc == "spotify":
        return spotify.playlist_membership(name)
    if svc == "tidal":
        return tidal.playlist_membership(name)
    if svc == "plex":
        return plex.playlist_membership(name)
    return set()


def _membership(svc: str, name: str, force: bool = False):
    """Cached set of artist|title keys in the service's playlist, or None if it
    can't be read right now (service down) and we have no prior snapshot."""
    now = time.time()
    cached = _members.get(svc)
    if not force and cached and now - cached[1] < _MEMBER_TTL:
        return cached[0]
    try:
        keys = _read_membership(svc, name)
    except Exception as e:
        log.debug("Auto-playlist: couldn't read %s playlist membership: %s", svc, e)
        return cached[0] if cached else None   # unknown
    _members[svc] = (keys, now)
    return keys


def _in_playlist(svc: str, name: str, key: str) -> bool:
    m = _membership(svc, name)
    return bool(m) and key in m


def _remember_member(svc: str, key: str) -> None:
    cached = _members.get(svc)
    if cached:
        cached[0].add(key)


def refresh_membership() -> None:
    """Force a re-read of every enabled service's playlist (e.g. after the name
    changes)."""
    _members.clear()


def _peak_queue(current: int) -> int:
    if current > _PEAK["n"]:
        _PEAK["n"] = current
    return _PEAK["n"]


def _in_backoff(svc: str) -> bool:
    return time.time() < _backoff_until.get(svc, 0)


def _backoff_base(svc: str) -> int:
    # Plex is local: recover quickly. Tidal is rate-limited: ease off harder.
    return 10 if svc == "plex" else config.AUTO_PLAYLIST_TIDAL_BACKOFF_SEC


def _note_error(svc: str) -> None:
    """Escalating backoff after an error response (rate limits, server down)."""
    n = _consec_fail.get(svc, 0) + 1
    _consec_fail[svc] = n
    base = _backoff_base(svc)
    wait = min(base * (2 ** (n - 1)), config.AUTO_PLAYLIST_TIDAL_BACKOFF_MAX_SEC)
    _backoff_until[svc] = time.time() + wait
    log.warning("Auto-playlist: backing off %s for %ds after an error (streak %d)",
                svc, wait, n)


def _is_unavailable(e: Exception) -> bool:
    """A service being unreachable (vs. a genuine per-track failure). These never
    count against a track's attempt budget -- the service will come back."""
    from .plex.client import PlexUnavailable
    if isinstance(e, PlexUnavailable):
        return True
    txt = f"{type(e).__name__}: {e}".lower()
    return any(k in txt for k in (
        "connection", "timeout", "timed out", "unreachable", "refused",
        "reset by peer", "temporarily unavailable", "bad gateway",
        "service unavailable", "not connected", "502", "503", "504",
        "429", "too many requests", "rate limit",
    ))


def _batch_for(svc: str) -> int:
    if svc == "plex":
        return config.AUTO_PLAYLIST_PLEX_BATCH
    return config.AUTO_PLAYLIST_TIDAL_BATCH


def _drain_service(svc: str, name: str) -> tuple:
    """Process one batch for a ready service. Returns
    (added, present, skipped, deferred, errored). Stops the batch on the first
    error so a rate-limited service can back off instead of hammering."""
    rows = db.autoplaylist_queue_pending(config.AUTO_PLAYLIST_MAX_ATTEMPTS,
                                         _batch_for(svc), service=svc)
    added = present = skipped = deferred = 0
    errored = False
    for row in rows:
        key = row["match_key"]
        try:
            status = _add_one(svc, name, row["artist"], row["title"], row.get("album"))
            db.autoplaylist_queue_remove(svc, key)
            if status == "added":
                added += 1
                _remember_member(svc, key)   # now in the playlist
                log.info("Auto-playlist: added %s - %s to %s",
                         row["artist"], row["title"], svc)
            elif status == "present":
                present += 1
                _remember_member(svc, key)   # already in the playlist
                log.info("Auto-playlist: %s - %s already in %s playlist (skipping)",
                         row["artist"], row["title"], svc)
            else:
                # Not on the service at all -> record it so we don't re-search it
                # every play. (This is the ONLY thing the log now gates.)
                skipped += 1
                db.autoplaylist_mark(svc, key)
                log.info("Auto-playlist: %s - %s not found on %s (skipping)",
                         row["artist"], row["title"], svc)
        except Exception as e:
            deferred += 1
            errored = True
            if _is_unavailable(e):
                # Service is down/rate-limited: leave the attempt count alone so a
                # long outage never exhausts a track's retries. It'll be picked up
                # again once the service is back.
                log.warning("Auto-playlist %s unavailable at %s - %s (will retry): %s",
                            svc, row["artist"], row["title"], e)
            else:
                db.autoplaylist_queue_attempt(svc, key)
                log.warning("Auto-playlist %s failed for %s - %s (will retry): %s",
                            svc, row["artist"], row["title"], e)
            break   # back off rather than keep hitting a struggling service
    if not errored and (added or present or skipped):
        _consec_fail[svc] = 0   # clean pass -> reset backoff escalation
    return added, present, skipped, deferred, errored


def flush(limit: int = 25) -> int:
    """Drain queued adds for each ready service. Plex drains a large chunk each
    cycle (as fast as the network allows); Tidal drains a smaller burst and backs
    off when it hits an error. Single-run: overlapping calls no-op. Returns tracks
    added this cycle."""
    if not _flush_lock.acquire(blocking=False):
        return 0
    try:
        name = config.AUTO_PLAYLIST_NAME
        added = present = skipped = deferred = 0
        for svc in _enabled_services():
            if not _service_ready(svc) or _in_backoff(svc):
                continue
            a, p, s, d, errored = _drain_service(svc, name)
            added += a; present += p; skipped += s; deferred += d
            if errored:
                _note_error(svc)
        if added or present or skipped or deferred:
            remaining = db.autoplaylist_queue_depth()
            done = _peak_queue(remaining + added + present + skipped) - remaining
            log.info("flush  +%d added  =%d already-in  -%d not-found  ~%d deferred",
                     added, present, skipped, deferred)
            log.info("queue  %s", logging_setup.bar(done, _PEAK["n"]))
        return added
    finally:
        _flush_lock.release()


def has_pending() -> bool:
    """Whether any queued item is ready to attempt now (used to pace the worker)."""
    for svc in _enabled_services():
        if _service_ready(svc) and not _in_backoff(svc):
            if db.autoplaylist_queue_pending(config.AUTO_PLAYLIST_MAX_ATTEMPTS, 1, service=svc):
                return True
    return False


def note_played(artist: str, title: str, album: str = None) -> None:
    """Called on each new now-playing track: queue it, then try to add promptly."""
    enqueue(artist, title, album)
    flush()


def backfill() -> int:
    """Queue every distinct track already in the archive (that isn't handled yet)
    for the enabled services. Returns how many (service, track) items were queued;
    a breakdown of what was skipped is left in last_backfill_stats()."""
    global _last_backfill
    svcs = _enabled_services()
    if not svcs:
        _last_backfill = {"queued": 0, "reason": "no service is enabled"}
        return 0
    queued = 0
    considered = below = in_playlist = absent = 0
    threshold = max(1, int(config.AUTO_PLAYLIST_MIN_PLAYS))
    name = config.AUTO_PLAYLIST_NAME
    for r in db.distinct_tracks_for_backfill():
        considered += 1
        if int(r.get("plays") or 0) < threshold:
            below += 1
            continue
        key = _key(r["artist"], r["title"])
        for svc in svcs:
            if _in_playlist(svc, name, key):
                in_playlist += 1
                continue
            if db.autoplaylist_seen(svc, key):
                absent += 1
                continue
            db.autoplaylist_enqueue(svc, key, r["artist"], r["title"], r.get("album"))
            queued += 1
    _last_backfill = {"queued": queued, "considered": considered,
                      "below_threshold": below, "already_in_playlist": in_playlist,
                      "not_on_service": absent, "min_plays": threshold}
    log.info("Auto-playlist backfill queued %d item(s) "
             "(%d tracks seen, %d under %d plays, %d already in playlist, "
             "%d not on the service)",
             queued, considered, below, threshold, in_playlist, absent)
    return queued


def last_backfill_stats() -> dict:
    return dict(_last_backfill)
