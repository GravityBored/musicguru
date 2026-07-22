"""On-disk cover-art cache.

cover_url points at Shazam's CDN. Those links rot -- in a year a good chunk of a
9,000-play archive would render as empty squares, and there'd be no way to get
the art back. Every cover the app has ever seen is copied here once, keyed by a
hash of its source URL, and served from /cover/<id> afterwards.

Files are re-encoded to JPEG and capped at config.COVER_MAX_PX, so ~10k covers costs
tens of megabytes rather than gigabytes.
"""
import hashlib
import logging
import os
import tempfile
from io import BytesIO

import requests
from PIL import Image

from . import config
from .storage import db

log = logging.getLogger("audio_recognition.covers")


def key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def path_for(url: str) -> str:
    return os.path.join(config.COVER_CACHE_DIR, key(url) + ".jpg")


def cached(url: str) -> str | None:
    if not url or not config.COVER_CACHE_ENABLED:
        return None
    p = path_for(url)
    return p if os.path.exists(p) and os.path.getsize(p) > 0 else None


def _encode(data: bytes) -> bytes | None:
    """Re-encode arbitrary image bytes to a size-capped RGB JPEG."""
    try:
        img = Image.open(BytesIO(data))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((config.COVER_MAX_PX, config.COVER_MAX_PX), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("Could not decode cover image: %s", e)
        return None


def _write_disk(dest: str, jpeg: bytes) -> None:
    os.makedirs(config.COVER_CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.COVER_CACHE_DIR, suffix=".jpg")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(jpeg)
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def store_bytes(url: str, data: bytes) -> str | None:
    """Re-encode once, then persist to BOTH the disk cache and the database.
    Called by the pipeline for free, since it already downloaded the art for
    the panel. The DB copy is the durable source of truth; the disk copy is a
    fast front layer that can be rebuilt from the DB."""
    if not url or not data or not config.COVER_CACHE_ENABLED:
        return None
    dest = path_for(url)
    on_disk = os.path.exists(dest) and os.path.getsize(dest) > 0
    if on_disk and (not config.COVER_DB_ENABLED or db.has_cover_blob(key(url))):
        return dest

    jpeg = _encode(data)
    if jpeg is None:
        return None
    try:
        if not on_disk:
            _write_disk(dest, jpeg)
        if config.COVER_DB_ENABLED:
            db.save_cover_blob(key(url), "image/jpeg", jpeg)
        return dest
    except Exception as e:
        log.warning("Could not cache cover %s: %s", url, e)
        return None


def _from_db_to_disk(url: str) -> str | None:
    """Rehydrate the disk cache from the DB blob, if present."""
    if not config.COVER_DB_ENABLED:
        return None
    blob = db.get_cover_blob(key(url))
    if not blob:
        return None
    dest = path_for(url)
    try:
        _write_disk(dest, blob[1])
        return dest
    except Exception as e:
        log.warning("Could not rehydrate cover %s from DB: %s", url, e)
        return None


def fetch(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=config.IMAGE_TIMEOUT, stream=True)
        if r.status_code != 200:
            log.info("Cover fetch got %s for %s", r.status_code, url)
            return None
        buf = BytesIO()
        for chunk in r.iter_content(64 * 1024):
            buf.write(chunk)
            if buf.tell() > config.IMAGE_MAX_BYTES:
                log.warning("Cover exceeds %d bytes; dropping", config.IMAGE_MAX_BYTES)
                return None
        return buf.getvalue()
    except requests.RequestException as e:
        log.info("Cover fetch failed for %s: %s", url, e)
        return None


def ensure(url: str) -> str | None:
    """Return a local path for this cover. Order of resolution: disk cache, then
    the DB blob (rehydrating disk), then a one-time download from the source.
    After the art has been seen once, this never touches the internet again."""
    if not url or not config.COVER_CACHE_ENABLED:
        return None
    hit = cached(url)
    if hit:
        return hit
    from_db = _from_db_to_disk(url)
    if from_db:
        return from_db
    data = fetch(url)
    if not data:
        return None
    return store_bytes(url, data)


def get_bytes(url: str) -> bytes | None:
    """Cover bytes for in-process use (e.g. the physical display), preferring
    disk, then the DB, then a one-time download. Lets the feh display avoid the
    internet once the art is cached, same as the web server."""
    if not url or not config.COVER_CACHE_ENABLED:
        return None
    p = cached(url) or _from_db_to_disk(url)
    if p:
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            pass
    data = fetch(url)
    if data:
        store_bytes(url, data)
    return data
