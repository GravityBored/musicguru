"""Optional Last.fm scrobbling of confirmed plays.

No-op unless AR_SCROBBLE is on and AR_LASTFM_API_KEY + AR_LASTFM_SECRET +
AR_LASTFM_SESSION_KEY are all set.

Getting a session key is a one-time interactive step:

    AR_LASTFM_API_KEY=... AR_LASTFM_SECRET=... \
        python -m audio_recognition.scrobble

follow the printed URL to authorize the app, press Enter, and paste the printed
value into AR_LASTFM_SESSION_KEY. Session keys don't expire.

Everything here is blocking `requests`; call from a worker thread so it never
stalls the capture loop.
"""
import hashlib
import logging

import requests

from . import config

log = logging.getLogger("audio_recognition.scrobble")
_URL = "https://ws.audioscrobbler.com/2.0/"


def enabled() -> bool:
    return bool(
        config.SCROBBLE_ENABLED
        and config.LASTFM_API_KEY
        and config.LASTFM_SECRET
        and config.LASTFM_SESSION_KEY
    )


def _sign(params: dict) -> str:
    """Last.fm api_sig: md5 of sorted key+value pairs plus the shared secret.
    format/callback are excluded from the signature by the API's rules."""
    joined = "".join(f"{k}{params[k]}" for k in sorted(params)) + config.LASTFM_SECRET
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def _post(method: str, **params) -> bool:
    params.update(
        {"method": method, "api_key": config.LASTFM_API_KEY, "sk": config.LASTFM_SESSION_KEY}
    )
    params["api_sig"] = _sign(params)
    params["format"] = "json"
    try:
        r = requests.post(_URL, data=params, timeout=config.LASTFM_TIMEOUT)
        if r.status_code != 200:
            log.warning("%s -> HTTP %s: %s", method, r.status_code, r.text[:120])
            return False
        body = r.json()
        if "error" in body:
            log.warning("%s -> Last.fm error %s: %s", method, body.get("error"), body.get("message"))
            return False
        return True
    except (requests.RequestException, ValueError) as e:
        log.warning("%s failed: %s", method, e)
        return False


def now_playing(artist, title, album=None, duration=None) -> None:
    if not enabled() or not artist or not title:
        return
    p = {"artist": artist, "track": title}
    if album:
        p["album"] = album
    if duration:
        p["duration"] = int(duration)
    _post("track.updateNowPlaying", **p)


def submit(artist, title, started_at, album=None, duration=None) -> None:
    """Scrobble a completed play. started_at is the UNIX time the track began."""
    if not enabled() or not artist or not title:
        return
    p = {"artist": artist, "track": title, "timestamp": int(started_at)}
    if album:
        p["album"] = album
    if duration:
        p["duration"] = int(duration)
    if _post("track.scrobble", **p):
        log.info("Scrobbled: %s - %s", artist, title)


# --- one-time session-key helper -----------------------------------------

def get_session_key() -> str:
    """Interactive desktop-auth flow. Prints the session key to stdout."""
    import webbrowser

    if not (config.LASTFM_API_KEY and config.LASTFM_SECRET):
        raise SystemExit("Set AR_LASTFM_API_KEY and AR_LASTFM_SECRET first.")

    sig = _sign({"method": "auth.getToken", "api_key": config.LASTFM_API_KEY})
    r = requests.get(
        _URL,
        params={"method": "auth.getToken", "api_key": config.LASTFM_API_KEY,
                "api_sig": sig, "format": "json"},
        timeout=10,
    )
    token = r.json()["token"]
    url = f"https://www.last.fm/api/auth/?api_key={config.LASTFM_API_KEY}&token={token}"
    print("Authorize the app here, then press Enter:\n ", url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    input()

    sig = _sign({"method": "auth.getSession", "api_key": config.LASTFM_API_KEY, "token": token})
    r = requests.get(
        _URL,
        params={"method": "auth.getSession", "api_key": config.LASTFM_API_KEY,
                "token": token, "api_sig": sig, "format": "json"},
        timeout=10,
    )
    sk = r.json()["session"]["key"]
    print("\nSuccess. Set this and restart:\n\n  AR_LASTFM_SESSION_KEY=" + sk + "\n")
    return sk


if __name__ == "__main__":
    get_session_key()
