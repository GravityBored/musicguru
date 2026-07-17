"""Offline verification of the deterministic logic in the new features.
No MySQL/Plex/Last.fm server is contacted; external calls are monkeypatched."""
import asyncio
import hashlib
import os
import sys
import tempfile
import types

# Allow `python tests/test_offline.py` from the repo root (add repo root to path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub audio/recognition libs that aren't needed for logic tests and may be
# absent in a bare environment (they're runtime-only deps of the daemon).
for name, attrs in {
    "pydub": {"AudioSegment": object},
    "pydub.effects": {"normalize": lambda *a, **k: None},
    "shazamio": {"Shazam": object},
}.items():
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

import audio_recognition.config as config

FAILS = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + extra if extra else ''}")
    if not cond:
        FAILS.append(name)


# 1) Last.fm api_sig construction -----------------------------------------
def test_sign():
    from audio_recognition import scrobble
    config.LASTFM_SECRET = "s3cr3t"
    params = {"method": "track.scrobble", "api_key": "KEY", "sk": "SESS",
              "artist": "Boards of Canada", "track": "Roygbiv", "timestamp": "1700000000"}
    got = scrobble._sign(params)
    raw = "".join(f"{k}{params[k]}" for k in sorted(params)) + "s3cr3t"
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest()
    check("scrobble._sign matches spec md5", got == expected)
    # 'format' must never be in the signed set; adding it changes the sig.
    p2 = dict(params, format="json")
    check("scrobble._sign is order-independent", scrobble._sign(p2) != got,
          "(format included changes it, as expected)")


# 2) corrections normalization + round trip -------------------------------
def test_corrections():
    from audio_recognition import corrections
    from audio_recognition.storage import db

    # Accent folding + parenthetical stripping should collapse variants.
    k1 = corrections.raw_key("Sigur Rós", "Hoppípolla (Remastered)")
    k2 = corrections.raw_key("Sigur Ros", "Hoppipolla")
    check("raw_key folds accents + parentheticals", k1 == k2, k1)

    # apply() round-trips through the in-memory map.
    db.load_corrections = lambda: {corrections.raw_key("Unknown", "Untitled"):
                                   ("Aphex Twin", "Xtal")}
    corrections.load()
    check("apply() rewrites a known miss",
          corrections.apply("Unknown", "Untitled") == ("Aphex Twin", "Xtal"))
    check("apply() leaves unknown pairs alone",
          corrections.apply("Real Artist", "Real Song") == ("Real Artist", "Real Song"))

    # add() must persist, memoize, and relabel.
    calls = {}
    db.save_correction = lambda rk, a, t: calls.setdefault("save", (rk, a, t))

    def _relabel(ot, oa, nt, na):
        calls["relabel"] = (ot, oa, nt, na)
        return 7
    db.relabel = _relabel
    n = corrections.add("Bad Artist", "Bad Title", "Good Artist", "Good Title")
    check("add() returns relabel count", n == 7)
    check("add() memoizes for future lookups",
          corrections.apply("Bad Artist", "Bad Title") == ("Good Artist", "Good Title"))
    check("add() relabels with (old_title, old_artist, new_title, new_artist)",
          calls["relabel"] == ("Bad Title", "Bad Artist", "Good Title", "Good Artist"))


# 3) spool replay ordering + partial failure ------------------------------
def test_spool():
    from audio_recognition.storage import db

    tmp = tempfile.mkdtemp()
    config.DB_SPOOL_FILE = os.path.join(tmp, "spool.jsonl")
    db.DB_SPOOL_FILE = config.DB_SPOOL_FILE  # module imported the name directly

    def row(t):
        return {"title": t, "artist": "A", "album": None, "genre": None,
                "duration": None, "cover_url": None}

    # DB down: three plays should spool, in order.
    db._insert = lambda r, ts=None: (False, None)
    for t in ("one", "two", "three"):
        rid = db.save_track(**row(t))
    with open(config.DB_SPOOL_FILE) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    check("save_track returns None when DB down", rid is None)
    check("all plays spooled in order", len(lines) == 3 and '"one"' in lines[0]
          and '"three"' in lines[2])
    check("spooled rows carry a ts", '"ts"' in lines[0])

    # DB half-up: first replay succeeds, second fails -> keep from the failure on.
    inserted = []
    state = {"n": 0}

    def flaky(r, ts=None):
        state["n"] += 1
        if state["n"] == 1:
            inserted.append(r["title"])
            return True, 101
        return False, None

    db._insert = flaky
    db._flush_spool()
    with open(config.DB_SPOOL_FILE) as f:
        rem = [l for l in f.read().splitlines() if l.strip()]
    check("first spooled row replayed", inserted == ["one"])
    check("failed row + remainder kept in order", len(rem) == 2
          and '"two"' in rem[0] and '"three"' in rem[1])

    # DB fully up: everything drains and the file is removed.
    db._insert = lambda r, ts=None: (True, 1)
    db._flush_spool()
    check("spool file removed once drained", not os.path.exists(config.DB_SPOOL_FILE))


# 4) publish graceful degradation -----------------------------------------
def test_publish():
    from audio_recognition import publish
    # No sinks configured -> silent no-op, no exception.
    config.NOWPLAYING_WEBHOOK = ""
    config.MQTT_HOST = ""
    publish.now_playing({"title": "x", "artist": "y"})
    publish.stopped()
    check("publish is a no-op with nothing configured", True)
    # MQTT host set but paho not installed -> _get_mqtt returns None, no raise.
    config.MQTT_HOST = "localhost"
    publish._mqtt = None
    publish._mqtt_tried = False
    check("MQTT falls back to None when paho missing", publish._get_mqtt() is None)


# 5) _finalize scrobble threshold -----------------------------------------
def test_finalize():
    from audio_recognition import main, scrobble

    def run(duration, secs, pid=55):
        rec = {"ls": None, "scrobbled": False}
        main.state.current_play_meta = lambda: (pid, 1000.0, {
            "artist": "A", "title": "T", "album": None, "duration": duration})
        main.update_listened_seconds = lambda p, s: rec.__setitem__("ls", (p, s))
        main.scrobble.submit = lambda *a, **k: rec.__setitem__("scrobbled", True)
        config.SCROBBLE_MIN_SECONDS = 30
        asyncio.run(main._finalize(1000.0 + secs))
        return rec

    r = run(None, 29)
    check("under 30s: listened_seconds written but no scrobble",
          r["ls"] == (55, 29) and r["scrobbled"] is False)
    r = run(None, 30)
    check("30s, no duration, floor=30: scrobbles", r["scrobbled"] is True)
    r = run(400, 100)
    check("100s of a 400s track (<50%): no scrobble", r["scrobbled"] is False)
    r = run(400, 200)
    check("200s of a 400s track (=50%): scrobbles", r["scrobbled"] is True)
    r = run(600, 300)
    check("300s of a 600s track (>4min cap): scrobbles", r["scrobbled"] is True)

    # No current track -> nothing happens, no exception.
    main.state.current_play_meta = lambda: (None, None, None)
    asyncio.run(main._finalize(1234.0))
    check("no current play: finalize is a no-op", True)


if __name__ == "__main__":
    for t in (test_sign, test_corrections, test_spool, test_publish, test_finalize):
        print(f"\n== {t.__name__} ==")
        t()
    print("\n" + ("ALL PASSED" if not FAILS else f"FAILURES: {FAILS}"))
    raise SystemExit(1 if FAILS else 0)
