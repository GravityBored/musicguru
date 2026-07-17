import atexit
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

from ..config import (
    COVER_ART_FILE,
    DISPLAY_ENABLED,
    DISPLAY_SIZE,
    FEH_RELOAD_SEC,
    FONT_PATH,
    FONT_SIZE,
    IMAGE_MAX_BYTES,
    IMAGE_RETRIES,
    IMAGE_TIMEOUT,
)

log = logging.getLogger("audio_recognition.display")

_feh_process: subprocess.Popen | None = None
_last_hash: str | None = None
_font: ImageFont.ImageFont | None = None


def _get_font():
    """The old call was ImageFont.truetype('DejaVuSans-Bold.ttf', 40) with a bare
    filename. PIL almost never finds that, so it silently fell back to
    load_default() -- a ~10px bitmap face, illegible on an 800x480 panel."""
    global _font
    if _font is None:
        try:
            _font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        except OSError:
            log.warning("Font not found at %s; falling back to PIL default", FONT_PATH)
            try:
                _font = ImageFont.load_default(size=FONT_SIZE)  # Pillow >= 10.1
            except TypeError:
                _font = ImageFont.load_default()
    return _font


def download_image_with_retries(url, retries=IMAGE_RETRIES, timeout=IMAGE_TIMEOUT):
    """Blocking. Call via asyncio.to_thread from the pipeline."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "")
                if ctype and not ctype.startswith("image/"):
                    log.warning("Cover URL returned %s, not an image", ctype)
                    return None
                buf = BytesIO()
                for chunk in resp.iter_content(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > IMAGE_MAX_BYTES:
                        log.warning("Cover art exceeds %d bytes; aborting", IMAGE_MAX_BYTES)
                        return None
                return buf.getvalue()
            log.warning("Attempt %d: image download failed with status %s", attempt, resp.status_code)
        except requests.RequestException as e:
            log.warning("Attempt %d: image download exception: %s", attempt, e)
        if attempt < retries:  # no pointless sleep after the final attempt
            time.sleep(1)
    return None


def display_text(text: str = "Identifying Audio") -> None:
    if not DISPLAY_ENABLED:
        return
    img = Image.new("RGB", DISPLAY_SIZE, "black")
    draw = ImageDraw.Draw(img)
    font = _get_font()
    # Subtract the bbox origin; the old code ignored it and drew slightly off-center.
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    w, h = x1 - x0, y1 - y0
    pos = ((DISPLAY_SIZE[0] - w) // 2 - x0, (DISPLAY_SIZE[1] - h) // 2 - y0)
    draw.text(pos, text, fill="white", font=font)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    resize_and_display(buf.getvalue())


def _atomic_save(canvas: Image.Image, path: str) -> None:
    """feh --reload polls the file; a partial write would show a torn image."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".jpg")
    os.close(fd)
    try:
        canvas.save(tmp, "JPEG", quality=90)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _ensure_viewer() -> None:
    """Start feh once and let it poll the file.

    The old code spawned a fresh feh per track and killed the previous one,
    which flickered to a black root window on every change and orphaned feh
    processes whenever terminate() failed.
    """
    global _feh_process
    if _feh_process is not None and _feh_process.poll() is None:
        return
    try:
        _feh_process = subprocess.Popen(
            [
                "feh",
                "--fullscreen",
                "--hide-pointer",
                "--reload", str(FEH_RELOAD_SEC),
                COVER_ART_FILE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.debug("Started feh (pid %s)", _feh_process.pid)
    except FileNotFoundError:
        log.error("feh not found on PATH; display disabled for this run")
        _feh_process = None


def resize_and_display(img_data: bytes) -> None:
    global _last_hash
    if not DISPLAY_ENABLED or not img_data:
        return

    curr_hash = hashlib.md5(img_data).hexdigest()
    if curr_hash == _last_hash and os.path.exists(COVER_ART_FILE):
        return

    try:
        img = Image.open(BytesIO(img_data))
        img.load()
    except Exception as e:
        log.warning("Could not decode cover art: %s", e)
        return

    if img.mode != "RGB":
        img = img.convert("RGB")

    sw, sh = DISPLAY_SIZE
    iw, ih = img.size
    if iw == 0 or ih == 0:
        return
    ratio = iw / ih
    if sw / sh > ratio:
        new_w, new_h = max(1, int(sh * ratio)), sh
    else:
        new_w, new_h = sw, max(1, int(sw / ratio))

    canvas = Image.new("RGB", DISPLAY_SIZE, "black")
    canvas.paste(img.resize((new_w, new_h), Image.LANCZOS), ((sw - new_w) // 2, (sh - new_h) // 2))

    try:
        _atomic_save(canvas, COVER_ART_FILE)
    except OSError as e:
        log.warning("Could not write %s: %s", COVER_ART_FILE, e)
        return

    _last_hash = curr_hash
    _ensure_viewer()


def shutdown_display() -> None:
    """The old code never killed feh, so it survived every restart."""
    global _feh_process
    if _feh_process is None:
        return
    proc, _feh_process = _feh_process, None
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


atexit.register(shutdown_display)
