"""Time helpers.

Rule for the whole codebase: every timestamp stored in SQLite is UTC ISO
("2026-09-19T14:30:00"); everything shown to a human is Asia/Singapore.
Dates (a dinner date, a duty Sunday) are stored as plain local ISO dates
because "Sunday the 21st" is a local-calendar fact, not an instant.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import config

LOCAL = ZoneInfo(config.TZ_NAME)
UTC = timezone.utc

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


# --- instants -------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def now_local() -> datetime:
    return now_utc().astimezone(LOCAL)


def to_local(dt: datetime) -> datetime:
    return _aware(dt).astimezone(LOCAL)


def to_utc(dt: datetime) -> datetime:
    return _aware(dt).astimezone(UTC)


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC - we never produce naive local times."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def iso(dt: datetime) -> str:
    """Serialise an instant for the DB (always UTC)."""
    return to_utc(dt).replace(microsecond=0).strftime(ISO_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=UTC)


def local_at(d: date, hour: int, minute: int = 0) -> datetime:
    """The instant of hour:minute on local date d, as an aware UTC datetime."""
    return datetime.combine(d, time(hour, minute), tzinfo=LOCAL).astimezone(UTC)


# --- calendar dates -------------------------------------------------------

def today_local() -> date:
    return now_local().date()


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def d(x: date | str) -> date:
    return parse_date(x) if isinstance(x, str) else x


def fmt_date(x: date | str) -> str:
    """'Fri 19 Sep' - weekday plus date, per spec 4.2."""
    dd = d(x)
    return f"{dd.strftime('%a')} {dd.day} {dd.strftime('%b')}"


def fmt_date_rel(x: date | str) -> str:
    """Same, but 'Today' / 'Tomorrow' when that is clearer."""
    dd = d(x)
    delta = (dd - today_local()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    return fmt_date(dd)


def fmt_datetime(dt: datetime) -> str:
    """'Thu 17 Sep, 11:42pm' in local time."""
    lt = to_local(dt)
    hour = lt.hour % 12 or 12
    ampm = "am" if lt.hour < 12 else "pm"
    return f"{fmt_date(lt.date())}, {hour}:{lt.minute:02d}{ampm}"


def date_range(days: int, start: date | None = None) -> list[date]:
    """`days` consecutive dates beginning at `start` (default tomorrow)."""
    first = start or (today_local() + timedelta(days=1))
    return [first + timedelta(days=i) for i in range(days)]


def next_sundays(count: int, start: date | None = None) -> list[date]:
    """The next `count` Sundays strictly after `start` (default today).

    Today is excluded even if it is a Sunday - you cannot roster someone
    for an evening that is already underway.
    """
    cur = (start or today_local()) + timedelta(days=1)
    while cur.weekday() != 6:  # Monday=0 ... Sunday=6
        cur += timedelta(days=1)
    return [cur + timedelta(weeks=i) for i in range(count)]


# --- quiet hours ----------------------------------------------------------

def _hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def in_quiet(dt: datetime, quiet_start: str = "23:00", quiet_end: str = "09:00") -> bool:
    """Is this instant inside the family's do-not-disturb window?"""
    lt = to_local(dt).time()
    start, end = _hhmm(quiet_start), _hhmm(quiet_end)
    if start <= end:                      # a same-day window
        return start <= lt < end
    return lt >= start or lt < end        # the usual overnight wrap


def shift_out_of_quiet(dt: datetime, quiet_start: str = "23:00",
                       quiet_end: str = "09:00") -> datetime:
    """Push an instant forward to the moment quiet hours end.

    Spec 8: skip, do not queue-burst. A nag that comes due at 02:00 fires
    once at 09:00, not five times at 09:00.
    """
    if not in_quiet(dt, quiet_start, quiet_end):
        return to_utc(dt)
    lt = to_local(dt)
    end = _hhmm(quiet_end)
    target = lt.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if target <= lt:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def window_bounds(day: date | str, window: str) -> tuple[datetime, datetime]:
    """Start and end instants of a coarse help-request window."""
    start_h, end_h, _, _ = config.WINDOWS[window]
    dd = d(day)
    return local_at(dd, start_h), local_at(dd, end_h)


def window_label(window: str) -> str:
    return config.WINDOWS[window][2]


def window_blurb(window: str) -> str:
    _, _, label, blurb = config.WINDOWS[window]
    return f"{label} ({blurb})"
