"""Console authentication.

Two independent, coexisting mechanisms:

* Interactive login -- a username/password form that sets a signed-cookie
  session. Enabled when AR_WEB_PASSWORD or AR_WEB_PASSWORD_HASH is set.
* Token -- AR_WEB_TOKEN in an X-Auth-Token header or ?token=, for machine
  callers (Prometheus, health checks) that can't hold a session.

A request is authorized if it has a valid session OR a valid token. If none of
password/hash/token is configured, auth is off and everything is open (the
loopback-by-default posture from before).

Generate a password hash:
    python -m audio_recognition.webapp.auth 'your password'
"""
import hashlib
import hmac

from werkzeug.security import check_password_hash, generate_password_hash

from .. import config


def login_enabled() -> bool:
    """Interactive login is available (a password is configured)."""
    return bool(config.WEB_PASSWORD or config.WEB_PASSWORD_HASH)


def needs_setup() -> bool:
    """True on first run: no credentials configured yet, so the user must create
    them before the console can be used."""
    return not login_enabled()


def enabled() -> bool:
    """Any auth at all is in force."""
    return bool(login_enabled() or config.WEB_TOKEN)


def check_login(user: str, password: str) -> bool:
    if not login_enabled():
        return False
    if not hmac.compare_digest(user or "", config.WEB_USER or ""):
        return False
    if config.WEB_PASSWORD_HASH:
        try:
            return check_password_hash(config.WEB_PASSWORD_HASH, password or "")
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(password or "", config.WEB_PASSWORD or "")


def check_token(supplied: str) -> bool:
    if not config.WEB_TOKEN or not supplied:
        return False
    return hmac.compare_digest(supplied, config.WEB_TOKEN)


def secret_key() -> bytes:
    """Stable key for signing session cookies. Prefers AR_WEB_SECRET_KEY; else
    derived from the configured password/token so logins survive a restart
    without extra config. Falls back to a random key when auth is off."""
    if config.WEB_SECRET_KEY:
        return config.WEB_SECRET_KEY.encode("utf-8")
    seed = config.WEB_PASSWORD_HASH or config.WEB_PASSWORD or config.WEB_TOKEN
    if seed:
        return hashlib.sha256(("ar-session:" + seed).encode("utf-8")).digest()
    import secrets
    return secrets.token_bytes(32)


if __name__ == "__main__":
    import getpass
    import sys

    pw = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("Password: ")
    print(generate_password_hash(pw))
    print("\nSet this in the environment as:")
    print("  AR_WEB_PASSWORD_HASH='<the line above>'")
