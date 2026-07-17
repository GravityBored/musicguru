"""Now-playing state shared between the recognition pipeline and the web thread.

Flask runs in a thread of the same process as loop_pipeline(), so this is just
a lock-guarded module global -- no IPC, no polling a file, no extra moving part.
"""
import threading
import time

_lock = threading.Lock()

_track: dict | None = None
_started_at: float = 0.0
_last_signal_at: float = 0.0   # last segment that had audio above the noise floor
_silent_segments: int = 0
_play_id: int | None = None    # DB id of the current play (for listened_seconds)
_context: dict = {}            # {"plays": N, "last_heard": "YYYY-..."} for the UI


def set_now_playing(track, play_id=None, context=None) -> None:
    """track is a recognize.shazam_client.Track."""
    global _track, _started_at, _last_signal_at, _silent_segments, _play_id, _context
    with _lock:
        _track = {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "genre": track.genre,
            "duration": track.duration,
            "cover_url": track.cover_url,
        }
        _started_at = time.time()
        _last_signal_at = _started_at
        _silent_segments = 0
        _play_id = play_id
        _context = context or {}


def note_signal() -> None:
    """Audio was present in the last segment (recognized or not)."""
    global _last_signal_at, _silent_segments
    with _lock:
        _last_signal_at = time.time()
        _silent_segments = 0


def note_silence() -> int:
    """Returns the running count of consecutive silent segments."""
    global _silent_segments
    with _lock:
        _silent_segments += 1
        return _silent_segments


def stop() -> None:
    global _track, _started_at, _silent_segments, _play_id, _context
    with _lock:
        _track = None
        _started_at = 0.0
        _silent_segments = 0
        _play_id = None
        _context = {}


def current_play_meta():
    """(_play_id, _started_at, copy of _track) for the current track, or
    (None, None, None). Used to finalize listened_seconds / scrobble on switch."""
    with _lock:
        if _track is None:
            return None, None, None
        return _play_id, _started_at, dict(_track)


def signal_age():
    """Seconds since audio was last seen, or None if never. Drives the watchdog
    and the /metrics gauge."""
    with _lock:
        if _last_signal_at <= 0:
            return None
        return time.time() - _last_signal_at


def snapshot() -> dict:
    with _lock:
        if _track is None:
            return {"playing": False, "signal": False}
        elapsed = int(time.time() - _started_at)
        return {
            "playing": True,
            # "signal" drives the lamp: audio seen in the last 20 seconds.
            "signal": (time.time() - _last_signal_at) < 20,
            "elapsed": elapsed,
            "plays": _context.get("plays"),
            "last_heard": _context.get("last_heard"),
            **_track,
        }
