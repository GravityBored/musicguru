"""UTC -> local formatting.

The old version assumed every naive datetime out of MySQL was UTC. If the
column was a TIMESTAMP and the server's time_zone was SYSTEM, the value came
back already local and got shifted a second time, putting "last played"
several hours in the future.

save_track() now writes recognized_at with UTC_TIMESTAMP() explicitly, so the
assumption holds. AR_DB_TIMES_ARE_UTC=0 disables the conversion for legacy rows.
"""
from datetime import datetime, timezone

try:
    from tzlocal import get_localzone
except ImportError:  # pragma: no cover
    get_localzone = None

from .. import config

_LOCAL_TZ = None


def _local_tz():
    global _LOCAL_TZ
    if _LOCAL_TZ is None:
        if get_localzone is not None:
            _LOCAL_TZ = get_localzone()
        else:
            _LOCAL_TZ = datetime.now().astimezone().tzinfo
    return _LOCAL_TZ


def utc_to_local_str(dt, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not dt:
        return ""
    if not isinstance(dt, datetime):
        return str(dt)
    if not config.DB_TIMES_ARE_UTC:
        return dt.strftime(fmt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz()).strftime(fmt)
