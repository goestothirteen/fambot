"""Spec 10: schedules survive a restart, and quiet hours are honoured."""
from datetime import timedelta


from app import db, dinner, scheduler
from app import timeutil as t
from tests.conftest import IDS


async def test_due_jobs_run_and_are_marked_done(bot):
    fired = []

    @scheduler.on("unit_probe")
    async def _probe(b, job):
        fired.append(job["ref_id"])

    db.add_job(t.now_utc() - timedelta(minutes=1), "unit_probe", 42, respect_quiet=False)
    assert await scheduler.run_due(bot) == 1
    assert fired == [42]
    assert db.q1("SELECT done FROM jobs WHERE kind='unit_probe'")["done"] == 1


async def test_future_jobs_are_left_alone(bot):
    db.add_job(t.now_utc() + timedelta(hours=2), "dinner_done", 1, respect_quiet=False)
    assert await scheduler.run_due(bot) == 0


async def test_a_failing_job_does_not_stall_the_queue(bot):
    ran = []

    @scheduler.on("unit_boom")
    async def _boom(b, job):
        raise RuntimeError("kaboom")

    @scheduler.on("unit_after")
    async def _after(b, job):
        ran.append(job["id"])

    db.add_job(t.now_utc() - timedelta(minutes=2), "unit_boom", 1, respect_quiet=False)
    db.add_job(t.now_utc() - timedelta(minutes=1), "unit_after", 2, respect_quiet=False)
    await scheduler.run_due(bot)
    assert ran, "the second job still ran"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE done=0") == 0


async def test_unknown_job_kinds_are_discarded_not_retried(bot):
    db.add_job(t.now_utc() - timedelta(minutes=1), "kind_from_an_old_version", 1,
               respect_quiet=False)
    await scheduler.run_due(bot)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE done=0") == 0


async def test_overdue_nag_waits_for_morning_instead_of_firing_at_3am(bot,
                                                                     monkeypatch):
    """The bot was offline overnight. It must not wake the house on boot."""
    await dinner.open_new(bot, IDS["mark"])
    db.x("UPDATE jobs SET due_at = ? WHERE kind='dinner_nag'",
         (t.iso(t.now_utc() - timedelta(hours=6)),))
    bot.reset()

    three_am = t.local_at(t.today_local(), 3, 0)
    monkeypatch.setattr(scheduler.t, "now_utc", lambda: three_am)
    monkeypatch.setattr(db.t, "now_utc", lambda: three_am)

    assert await scheduler.run_due(bot) == 0
    assert bot.sent == []
    row = db.q1("SELECT due_at, done FROM jobs WHERE kind='dinner_nag'")
    assert row["done"] == 0
    assert t.to_local(t.parse_iso(row["due_at"])).hour == 9


async def test_time_critical_jobs_still_run_during_quiet_hours(bot, monkeypatch):
    """A dinner-done sweep at 22:00 is not a notification; it must not slip."""
    ran = []

    @scheduler.on("unit_silent")
    async def _silent(b, job):
        ran.append(1)

    db.add_job(t.now_utc() - timedelta(minutes=1), "unit_silent", 1, respect_quiet=False)
    two_am = t.local_at(t.today_local(), 2, 0)
    monkeypatch.setattr(scheduler.t, "now_utc", lambda: two_am)
    monkeypatch.setattr(db.t, "now_utc", lambda: two_am)
    await scheduler.run_due(bot)
    assert ran == [1]


async def test_state_survives_a_restart(bot, tmp_path):
    """Close the DB as a container stop would, reopen it, and carry on."""
    pid = await dinner.open_new(bot, IDS["mark"])
    day = [d.isoformat() for d in dinner.poll_days(
        db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,)))][0]
    for uid in IDS.values():
        db.x("INSERT OR IGNORE INTO dinner_votes (poll_id, user_id, vote_date) "
             "VALUES (?, ?, ?)", (pid, uid, day))
    path = db.conn().execute("PRAGMA database_list").fetchone()[2]

    db.close()                      # <-- docker compose restart
    db.init(path)

    poll = dinner.open_poll()
    assert poll is not None and poll["id"] == pid
    assert dinner.viable_days(poll) == [day]
    # The nag and deadline are still queued, because they live in the table.
    assert db.has_pending_job("dinner_nag", pid)
    assert db.has_pending_job("dinner_deadline", pid)


async def test_jobs_missed_during_downtime_run_on_the_next_tick(bot, monkeypatch):
    await dinner.open_new(bot, IDS["mark"])
    real_now = t.now_utc()
    db.x("UPDATE jobs SET due_at = ? WHERE kind='dinner_nag'",
         (t.iso(real_now - timedelta(hours=9)),))
    bot.reset()

    # Pin the clock to the afternoon so the result does not depend on when
    # the suite happens to be run.
    afternoon = t.local_at(t.today_local(), 15, 0)
    monkeypatch.setattr(t, "now_utc", lambda: afternoon)

    assert await scheduler.run_due(bot) >= 1
    assert bot.said("Still waiting on")   # the 6-hourly nag went out
