import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from .config import LOG_FILE, LOG_LEVEL

_configured = False


def setup_logging(level=None):
    """Configure the 'audio_recognition' logger. Safe to call more than once."""
    global _configured
    log = logging.getLogger("audio_recognition")
    if _configured:
        return log

    level = level or getattr(logging, LOG_LEVEL, logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    handlers = [console]

    try:
        parent = os.path.dirname(LOG_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fh = TimedRotatingFileHandler(LOG_FILE, when="D", interval=1, backupCount=7)
        fh.setFormatter(fmt)
        handlers.append(fh)
    except OSError as e:
        # Previously this raised at import time and killed the process.
        console.handle(
            logging.LogRecord(
                "audio_recognition", logging.WARNING, __file__, 0,
                "File logging disabled (%s): %s", (LOG_FILE, e), None,
            )
        )

    log.setLevel(level)
    log.propagate = False
    for h in handlers:
        log.addHandler(h)

    _configured = True
    return log
