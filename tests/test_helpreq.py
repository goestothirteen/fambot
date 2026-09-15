"""Feature B acceptance: dog walk and home coverage (spec 10)."""
from datetime import timedelta

from app import db, helpreq, scheduler, tg
from app import timeutil as t
from tests.conftest import IDS, ctx, fake_update

TOMORROW = None


def tmr():
    return (t.today_local() + timedelta(days=1)).isoformat()


async def make(bot, kind="dog_takeover", who="dad", day=None, window="evening",
               label=None):
    return await helpreq.create(bot, kind, IDS[who], day or tmr(), window, label)


async def test_request_is_posted_with_a_claim_button(bot):
    rid = await make(bot)
    assert bot.said("needs help with Rush")
    assert bot.said("Take over the walk")
    assert f"h|claim|{rid}" in bot.callbacks()


async def test_coverage_request_carries_its_task_label(bot):
    await make(bot, kind="coverage", who="mom", label="both")
    assert bot.said("needs someone home")
    assert bot.said("watch the dogs and the house")


async def test_takeover_is_driver_only(bot):
    rid = await make(bot, kind="dog_takeover")
    upd = fake_update(f"h|claim|{rid}", IDS["dawn"])       # Dawn does not drive
    await helpreq.on_callback(upd, ctx(bot))
    assert "needs a driver" in upd.answers[-1]["text"]
    assert db.q1("SELECT status FROM help_requests WHERE id=?", (rid,))["status"] == "open"


async def test_accompany_is_open_to_any_kid(bot):
    rid = await make(bot, kind="dog_accompany")
    await helpreq.on_callback(fake_update(f"h|claim|{rid}", IDS["dawn"]), ctx(bot))
    row = db.q1("SELECT * FROM help_requests WHERE id=?", (rid,))
    assert row["status"] == "claimed" and row["claimed_by"] == IDS["dawn"]


async def test_driver_can_take_over(bot):
    rid = await make(bot, kind="dog_takeover")
    await helpreq.on_callback(fake_update(f"h|claim|{rid}", IDS["mark"]), ctx(bot))
    assert db.q1("SELECT status FROM help_requests WHERE id=?", (rid,))["status"] == "claimed"
    assert bot.said("Mark's got it")


async def test_first_tap_wins(bot):
    rid = await make(bot, kind="coverage", who="mom", label="dogs")
    assert helpreq.claim(rid, IDS["dawn"]) is True
    assert helpreq.claim(rid, IDS["luke"]) is False
    assert db.q1("SELECT claimed_by FROM help_requests WHERE id=?",
                 (rid,))["claimed_by"] == IDS["dawn"]


async def test_late_tapper_is_told_it_is_taken(bot):
    rid = await make(bot, kind="dog_accompany")
    helpreq.claim(rid, IDS["dawn"])
    upd = fake_update(f"h|claim|{rid}", IDS["luke"])
    await helpreq.on_callback(upd, ctx(bot))
    assert "already grabbed" in upd.answers[-1]["text"]


async def test_requester_cannot_claim_their_own_request(bot):
    rid = await make(bot, kind="dog_accompany")
    upd = fake_update(f"h|claim|{rid}", IDS["dad"])
    await helpreq.on_callback(upd, ctx(bot))
    assert "the one who asked" in upd.answers[-1]["text"]


async def test_claiming_credits_the_duty_log_and_stops_repings(bot):
    rid = await make(bot, kind="dog_takeover")
    helpreq.claim(rid, IDS["mark"])
    assert db.duty_counts()[IDS["mark"]] == 1
    assert not db.has_pending_job("help_reping", rid)
    assert db.has_pending_job("help_remind", rid)


async def test_unclaim_reopens_and_removes_the_credit(bot):
    rid = await make(bot, kind="dog_takeover")
    helpreq.claim(rid, IDS["mark"])
    bot.reset()

    await helpreq.on_callback(fake_update(f"h|drop|{rid}", IDS["mark"]), ctx(bot))
    row = db.q1("SELECT * FROM help_requests WHERE id=?", (rid,))
    assert row["status"] == "open" and row["claimed_by"] is None
    assert db.duty_counts().get(IDS["mark"], 0) == 0
    assert db.has_pending_job("help_reping", rid)
    assert bot.said("can't do it any more")


async def test_only_the_claimant_can_unclaim(bot):
    rid = await make(bot, kind="dog_accompany")
    helpreq.claim(rid, IDS["dawn"])
    upd = fake_update(f"h|drop|{rid}", IDS["luke"])
    await helpreq.on_callback(upd, ctx(bot))
    assert db.q1("SELECT status FROM help_requests WHERE id=?", (rid,))["status"] == "claimed"


async def test_reping_targets_drivers_only_for_a_takeover(bot):
    rid = await make(bot, kind="dog_takeover")
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='help_reping' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["help_reping"](bot, job)
    msg = bot.last
    assert "Mark" in msg
    assert "Dawn" not in msg and "Luke" not in msg


async def test_reping_targets_all_kids_for_coverage(bot):
    rid = await make(bot, kind="coverage", who="mom", label="house")
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='help_reping' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["help_reping"](bot, job)
    for name in ("Mark", "Dawn", "Luke"):
        assert name in bot.last


async def test_reping_escalates_and_rearms(bot):
    rid = await make(bot, kind="coverage", who="mom", label="dogs")
    job = db.q1("SELECT * FROM jobs WHERE kind='help_reping' AND ref_id=?", (rid,))
    db.finish_job(job["id"])
    await scheduler.HANDLERS["help_reping"](bot, job)
    nxt = db.q1("SELECT * FROM jobs WHERE kind='help_reping' AND ref_id=? AND done=0", (rid,))
    assert nxt["payload"] == "2"

    bot.reset()
    db.finish_job(nxt["id"])
    await scheduler.HANDLERS["help_reping"](bot, nxt)
    assert "Still nobody" in bot.last


async def test_reping_goes_quiet_once_claimed(bot):
    rid = await make(bot, kind="dog_accompany")
    helpreq.claim(rid, IDS["dawn"])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='help_reping' AND ref_id=?", (rid,))
    if job:
        await scheduler.HANDLERS["help_reping"](bot, job)
    assert bot.sent == []


async def test_unclaimed_request_expires_at_the_window(bot):
    rid = await make(bot, kind="dog_accompany")
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='help_expire' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["help_expire"](bot, job)
    assert db.q1("SELECT status FROM help_requests WHERE id=?", (rid,))["status"] == "expired"
    assert bot.said("Nobody could help")


async def test_claimant_reminder_names_the_job(bot):
    rid = await make(bot, kind="dog_takeover", day=(t.today_local() + timedelta(days=3)).isoformat())
    helpreq.claim(rid, IDS["mark"])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='help_remind' AND ref_id=? AND payload='before'",
                (rid,))
    await scheduler.HANDLERS["help_remind"](bot, job)
    assert "taking Rush out for Dad" in bot.last


async def test_cancel_is_limited_to_requester_or_admin(bot):
    rid = await make(bot, kind="dog_accompany", who="dad")
    ok, msg = helpreq.cancel(rid, IDS["dawn"])
    assert ok is False and "Only the person who asked" in msg
    ok, _ = helpreq.cancel(rid, IDS["mark"])            # Mark is admin
    assert ok is True
    assert db.q1("SELECT status FROM help_requests WHERE id=?",
                 (rid,))["status"] == "cancelled"
    assert not db.has_pending_job("help_reping", rid)
    assert not db.has_pending_job("help_expire", rid)


async def test_open_list_shows_only_unclaimed(bot):
    a = await make(bot, kind="dog_accompany")
    b = await make(bot, kind="coverage", who="mom", label="house", window="morning")
    helpreq.claim(a, IDS["dawn"])
    assert [r["id"] for r in helpreq.open_requests()] == [b]


async def test_request_wizard_builds_the_right_callbacks(bot):
    await helpreq.on_callback(fake_update("h|new|dog", IDS["dad"]), ctx(bot))
    assert "h|d|dog|" in " ".join(bot.callbacks())
    day = tmr()
    await helpreq.on_callback(fake_update(f"h|d|dog|{day}", IDS["dad"]), ctx(bot))
    assert f"h|w|dog|{day}|evening" in bot.callbacks()
    await helpreq.on_callback(fake_update(f"h|w|dog|{day}|evening", IDS["dad"]), ctx(bot))
    assert f"h|mk|dog_takeover|{day}|evening" in bot.callbacks()
    await helpreq.on_callback(
        fake_update(f"h|mk|dog_takeover|{day}|evening", IDS["dad"]), ctx(bot))
    assert db.scalar("SELECT COUNT(*) FROM help_requests") == 1


async def test_callback_tokens_stay_inside_the_64_byte_budget(bot):
    day = tmr()
    for token in [tg.cb("h", "mk", "coverage", day, "afternoon", "both"),
                  tg.cb("h", "w", "cov", day, "afternoon"),
                  tg.cb("d", "lock", 99999, day),
                  tg.cb("r", "av", 99999, day)]:
        assert len(token.encode()) <= 64
