"""Best-effort notifications (used by the capture watchdog).

Posts to AR_NOTIFY_URL: JSON {"title","message"} for a generic webhook, or a
plain-text body with a Title header if the URL looks like an ntfy topic. With no
URL set it just logs. Never raises.
"""
import logging

import requests

from . import config

log = logging.getLogger("audio_recognition.notify")


def send(title: str, message: str) -> None:
    if not config.NOTIFY_URL:
        log.warning("[notify] %s: %s", title, message)
        return
    try:
        if "ntfy" in config.NOTIFY_URL:
            requests.post(
                config.NOTIFY_URL,
                data=message.encode("utf-8"),
                headers={"Title": title},
                timeout=config.NOTIFY_TIMEOUT,
            )
        else:
            requests.post(
                config.NOTIFY_URL,
                json={"title": title, "message": message},
                timeout=config.NOTIFY_TIMEOUT,
            )
    except requests.RequestException as e:
        log.warning("notify failed: %s", e)
