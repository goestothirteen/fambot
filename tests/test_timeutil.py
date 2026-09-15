from datetime import date, timedelta

from app import timeutil as t


def test_quiet_hours_wrap_midnight():
    assert t.in_quiet(t.local_at(date(2026, 9, 19), 23, 30))
    assert t.in_quiet(t.local_at(date(2026, 9, 19), 2, 0))
    assert t.in_quiet(t.local_at(date(2026, 9, 19), 8, 59))
    assert not t.in_quiet(t.local_at(date(2026, 9, 19), 9, 0))
    assert not t.in_quiet(t.local_at(date(2026, 9, 19), 22, 59))


def test_shift_out_of_quiet_lands_on_nine_am():
    late = t.local_at(date(2026, 9, 19), 23, 30)
    assert t.to_local(t.shift_out_of_quiet(late)) == t.to_local(
        t.local_at(date(2026, 9, 20), 9, 0))

    early = t.local_at(date(2026, 9, 19), 3, 15)
    assert t.to_local(t.shift_out_of_quiet(early)) == t.to_local(
        t.local_at(date(2026, 9, 19), 9, 0))


def test_shift_leaves_waking_hours_alone():
    noon = t.local_at(date(2026, 9, 19), 12, 0)
    assert t.shift_out_of_quiet(noon) == t.to_utc(noon)


def test_next_sundays_excludes_today_even_if_sunday():
    # 2026-09-20 is a Sunday.
    got = t.next_sundays(3, start=date(2026, 9, 20))
    assert [d.isoformat() for d in got] == ["2026-09-27", "2026-10-04", "2026-10-11"]
    assert all(d.weekday() == 6 for d in got)


def test_next_sundays_from_midweek():
    got = t.next_sundays(2, start=date(2026, 9, 16))   # a Wednesday
    assert [d.isoformat() for d in got] == ["2026-09-20", "2026-09-27"]


def test_date_range_starts_tomorrow_by_default():
    got = t.date_range(7)
    assert got[0] == t.today_local() + timedelta(days=1)
    assert len(got) == 7


def test_formatting_is_human():
    assert t.fmt_date("2026-09-19") == "Sat 19 Sep"
    assert t.fmt_date_rel(t.today_local()) == "Today"
    assert t.fmt_date_rel(t.today_local() + timedelta(days=1)) == "Tomorrow"
    assert t.fmt_datetime(t.local_at(date(2026, 9, 17), 23, 42)) == "Thu 17 Sep, 11:42pm"
    assert t.fmt_datetime(t.local_at(date(2026, 9, 17), 0, 5)) == "Thu 17 Sep, 12:05am"


def test_iso_roundtrip_is_utc():
    now = t.now_utc()
    assert t.parse_iso(t.iso(now)) == now


def test_window_bounds_are_local():
    start, end = t.window_bounds("2026-09-19", "evening")
    assert t.to_local(start).hour == 17
    assert t.to_local(end).hour == 21
