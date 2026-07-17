"""Local recognition cache.

Identify a track that has been heard before WITHOUT calling Shazam.

Each identified segment is fingerprinted with Chromaprint (the `fpcalc` binary,
the same fingerprinter behind AcoustID/MusicBrainz). The raw fingerprint -- a
sequence of 32-bit frame hashes -- is stored per track. On a later segment we
fingerprint it and match against the stored ones; a close match returns the
cached identity and skips the network round-trip to Shazam entirely.

Matching stays cheap as the library grows via an inverted index: each stored
frame hash (minus its 8 noisiest low bits) points back at the fingerprints that
contain it, so a query only ever bit-compares against a handful of candidates
instead of the whole corpus.

If `fpcalc` isn't installed this module reports unavailable and the pipeline
falls back to Shazam for everything, exactly as before. On Debian/Ubuntu/RPi OS:
`sudo apt install libchromaprint-tools`.
"""
import logging
import shutil
import subprocess
import threading

from . import config
from .storage import db

log = logging.getLogger("audio_recognition.fingerprint")

_SUB_MASK = 0xFFFFFF          # index on the top 24 bits (drop 8 noisy low bits)
_CANDIDATES = 12              # full-compare at most this many index candidates
_MIN_OVERLAP = 15            # frames of overlap required for a valid alignment
_MAX_OFFSET = 25             # search this many frame offsets each direction

_lock = threading.Lock()
_index: dict[int, list[int]] = {}     # sub-hash -> [fp_id, ...]
_fps: dict[int, tuple[str, list[int]]] = {}   # fp_id -> (match_key, ints)
_known: dict[str, dict] = {}          # match_key -> track meta
_next_id = 1
_fpcalc = None
_checked = False


# --- availability + compute ----------------------------------------------

def available() -> bool:
    global _fpcalc, _checked
    if not config.LOCAL_RECOGNITION:
        return False
    if not _checked:
        _fpcalc = shutil.which("fpcalc")
        _checked = True
        if not _fpcalc:
            log.warning("fpcalc not found; local recognition disabled "
                        "(install libchromaprint-tools to enable).")
    return _fpcalc is not None


def compute(path: str) -> list[int] | None:
    """Raw Chromaprint fingerprint of an audio file, as a list of ints."""
    if not available():
        return None
    try:
        out = subprocess.run(
            [_fpcalc, "-raw", "-length", str(config.FP_LENGTH_SEC), path],
            capture_output=True, text=True, timeout=config.FP_LENGTH_SEC + 8,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("fpcalc failed: %s", e)
        return None
    for line in out.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            try:
                return [int(x) for x in line[12:].split(",") if x]
            except ValueError:
                return None
    return None


# --- similarity ----------------------------------------------------------

def _similarity(a: list[int], b: list[int]) -> float:
    """Best fraction of matching bits over any frame alignment of a and b."""
    la, lb = len(a), len(b)
    best = 0.0
    for off in range(-_MAX_OFFSET, _MAX_OFFSET + 1):
        start = max(0, -off)
        end = min(la, lb - off)
        n = end - start
        if n < _MIN_OVERLAP:
            continue
        diff = 0
        for i in range(start, end):
            diff += (a[i] ^ b[i + off]).bit_count()
        sim = 1.0 - diff / (n * 32)
        if sim > best:
            best = sim
    return best


# --- index ---------------------------------------------------------------

def load() -> None:
    """Warm the in-memory index from the database at startup."""
    global _index, _fps, _known, _next_id
    known = db.load_known_tracks()
    fps = db.load_fingerprints()   # [(fp_id, match_key, ints), ...]
    with _lock:
        _known = known
        _index, _fps = {}, {}
        mx = 0
        for fp_id, key, ints in fps:
            _fps[fp_id] = (key, ints)
            for v in ints:
                _index.setdefault((v >> 8) & _SUB_MASK, []).append(fp_id)
            mx = max(mx, fp_id)
        _next_id = mx + 1
    log.info("Local recognition primed: %d tracks, %d fingerprints.",
             len(known), len(fps))


def match(ints: list[int]) -> tuple[dict, float] | None:
    """Return (track meta, score) for the best local match above the configured
    threshold, or None. meta is a dict with title/artist/album/genre/duration/
    cover_url."""
    if not ints:
        return None
    with _lock:
        counts: dict[int, int] = {}
        for v in ints:
            for fid in _index.get((v >> 8) & _SUB_MASK, ()):
                counts[fid] = counts.get(fid, 0) + 1
        if not counts:
            return None
        cands = sorted(counts, key=counts.get, reverse=True)[:_CANDIDATES]
        snapshot = [_fps[fid] for fid in cands]   # each is (match_key, ints)
        known = dict(_known)
    best_key, best_score = None, 0.0
    for key, fp in snapshot:
        s = _similarity(ints, fp)
        if s > best_score:
            best_score, best_key = s, key
    if best_key and best_score >= config.FP_MATCH_THRESHOLD:
        meta = known.get(best_key)
        if meta:
            return meta, best_score
    return None


def remember(match_key: str, meta: dict, ints: list[int]) -> None:
    """Persist a track's metadata and this segment's fingerprint for future
    local hits. Skips a near-duplicate fingerprint already stored for the track."""
    if not available() or not match_key or not ints:
        return
    # Refresh the canonical metadata for this identity.
    db.upsert_known_track(match_key, meta)
    with _lock:
        _known[match_key] = dict(meta)
        # Don't store a fingerprint we already effectively have.
        for fid, (k, fp) in _fps.items():
            if k == match_key and _similarity(ints, fp) >= 0.95:
                return
    fp_id = db.add_fingerprint(match_key, ints, config.FP_MAX_PER_TRACK)
    if fp_id is None:
        return
    with _lock:
        _known[match_key] = dict(meta)
        _fps[fp_id] = (match_key, ints)
        for v in ints:
            _index.setdefault((v >> 8) & _SUB_MASK, []).append(fp_id)
