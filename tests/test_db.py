from datetime import timedelta

from app import db
from app import timeutil as t
from tests.conftest import IDS


def test_members_seeded_with_roles():
    names = [m["name"] for m in db.members()]
    assert names == ["Dad", "Mom", "Mark", "Dawn", "Luke"]
    assert [m["name"] for m in db.kids()] == ["Mark", "Dawn", "Luke"]
    assert db.is_driver(IDS["mark"]) and db.is_driver(IDS["dad"])
    assert not db.is_driver(IDS["dawn"])
    assert db.is_admin(IDS["mark"]) and not db.is_admin(IDS["mom"])


def test_seeding_is_idempotent():
    before = len(db.members())
    db.seed_members()
    assert len(db.members()) == before


def test_one_telegram_id_maps_to_one_person():
    db.register_member("dawn", IDS["luke"])          # Dawn taps on Luke's phone
    assert db.member_by_slug("luke")["user_id"] is None
    assert db.member_by_slug("dawn")["user_id"] == IDS["luke"]


def test_duty_counts_respect_the_trailing_window():
    recent = (t.today_local() - timedelta(weeks=2)).isoformat()
    ancient = (t.today_local() - timedelta(weeks=40)).isoformat()
    db.log_duty(IDS["mark"], recent, "roster")
    db.log_duty(IDS["mark"], ancient, "roster")
    db.log_duty(IDS["dawn"], recent, "coverage")
    counts = db.duty_counts(weeks=8)
    assert counts[IDS["mark"]] == 1          # the 40-week-old one drops out
    assert counts[IDS["dawn"]] == 1


def test_duty_log_does_not_double_count_the_same_duty():
    day = t.today_local().isoformat()
    db.log_duty(IDS["mark"], day, "dog_takeover", 7)
    db.log_duty(IDS["mark"], day, "dog_takeover", 7)
    assert db.duty_counts()[IDS["mark"]] == 1


def test_jobs_are_deferred_out_of_quiet_hours_when_queued():
    from datetime import date
    db.add_job(t.local_at(date(2026, 9, 19), 2, 0), "dinner_nag", 1)
    row = db.q1("SELECT due_at FROM jobs")
    assert t.to_local(t.parse_iso(row["due_at"])).hour == 9


def test_jobs_can_opt_out_of_quiet_hours():
    from datetime import date
    when = t.local_at(date(2026, 9, 19), 2, 0)
    db.add_job(when, "dinner_done", 1, respect_quiet=False)
    row = db.q1("SELECT due_at FROM jobs")
    assert t.parse_iso(row["due_at"]) == t.to_utc(when)


def test_due_jobs_only_returns_the_ripe_ones():
    db.add_job(t.now_utc() - timedelta(minutes=5), "dinner_done", 1, respect_quiet=False)
    db.add_job(t.now_utc() + timedelta(hours=5), "dinner_done", 2, respect_quiet=False)
    due = db.due_jobs()
    assert [j["ref_id"] for j in due] == [1]


def test_cancel_jobs_scopes_to_kind_and_ref():
    db.add_job(t.now_utc(), "dinner_nag", 1, respect_quiet=False)
    db.add_job(t.now_utc(), "dinner_nag", 2, respect_quiet=False)
    db.add_job(t.now_utc(), "help_reping", 1, respect_quiet=False)
    assert db.cancel_jobs("dinner_nag", 1) == 1
    assert db.has_pending_job("dinner_nag", 2)
    assert db.has_pending_job("help_reping", 1)
    assert not db.has_pending_job("dinner_nag", 1)


def test_settings_fall_back_to_defaults():
    assert db.get_int("dinner_nag_hours") == 6
    db.set_setting("dinner_nag_hours", 3)
    assert db.get_int("dinner_nag_hours") == 3
