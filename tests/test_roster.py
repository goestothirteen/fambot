"""Feature C acceptance: the rolling Sunday roster (spec 10)."""
from datetime import timedelta

from app import db, roster, scheduler, tg
from app import timeutil as t
from tests.conftest import IDS, ctx, fake_update, settle

MARK, DAWN, LUKE = IDS["mark"], IDS["dawn"], IDS["luke"]


# --- the fairness rule, as a pure function --------------------------------

def test_fairness_prefers_whoever_has_done_least():
    out = roster.assign_slots(
        ["2026-09-20"], {"2026-09-20": [MARK, DAWN, LUKE]},
        counts={MARK: 4, DAWN: 1, LUKE: 3}, last_duty={})
    assert out["2026-09-20"] == DAWN


def test_fairness_tie_breaks_on_longest_ago():
    out = roster.assign_slots(
        ["2026-09-20"], {"2026-09-20": [MARK, DAWN, LUKE]},
        counts={MARK: 2, DAWN: 2, LUKE: 2},
        last_duty={MARK: "2026-09-06", DAWN: "2026-07-01", LUKE: "2026-08-15"})
    assert out["2026-09-20"] == DAWN


def test_fairness_spreads_within_one_round():
    """Assignments made in this round count immediately, so nobody hoards."""
    days = ["2026-09-20", "2026-09-27", "2026-10-04"]
    out = roster.assign_slots(days, {d: [MARK, DAWN, LUKE] for d in days},
                              counts={}, last_duty={},
                              jitter={MARK: 0.1, DAWN: 0.2, LUKE: 0.3})
    assert sorted(out.values()) == sorted([MARK, DAWN, LUKE])


def test_one_kid_can_hold_several_sundays_when_others_are_busy():
    days = ["2026-09-20", "2026-09-27"]
    out = roster.assign_slots(days, {d: [LUKE] for d in days}, {}, {})
    assert out == {"2026-09-20": LUKE, "2026-09-27": LUKE}


def test_a_sunday_nobody_marked_comes_back_unfilled():
    out = roster.assign_slots(["2026-09-20"], {"2026-09-20": []}, {}, {})
    assert out["2026-09-20"] is None


def test_existing_history_carries_into_the_round():
    """Someone far behind keeps catching up before the rota alternates again."""
    days = ["2026-09-20", "2026-09-27", "2026-10-04"]
    out = roster.assign_slots(days, {d: [MARK, DAWN] for d in days},
                              counts={MARK: 3, DAWN: 0}, last_duty={},
                              jitter={MARK: 0.1, DAWN: 0.2})
    # Dawn is three behind, so she takes all three before they are level.
    assert list(out.values()) == [DAWN, DAWN, DAWN]

    # Once level, the next Sunday goes to Mark on the longest-ago tie-break.
    out2 = roster.assign_slots(
        ["2026-10-11"], {"2026-10-11": [MARK, DAWN]},
        counts={MARK: 3, DAWN: 3},
        last_duty={MARK: "2026-08-01", DAWN: "2026-10-04"})
    assert out2["2026-10-11"] == MARK


# --- rounds ----------------------------------------------------------------

async def mark_free(bot, rid, slug, day):
    upd = fake_update(f"r|av|{rid}|{day}", IDS[slug])
    await roster.on_callback(upd, ctx(bot))
    await tg.debouncer.flush()
    return upd


async def test_setup_starts_a_round_today(bot):
    rid = await roster.start_round(bot)
    assert rid is not None
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,))
    dates = roster.round_dates(rnd)
    assert len(dates) == 5
    assert all(t.parse_date(d).weekday() == 6 for d in dates)
    assert bot.said("SUNDAY DUTY")
    assert db.scalar("SELECT COUNT(*) FROM roster_slots") == 5


async def test_second_round_is_refused_while_one_is_open(bot):
    await roster.start_round(bot)
    assert await roster.start_round(bot) is None
    assert db.scalar("SELECT COUNT(*) FROM roster_rounds") == 1


async def test_only_kids_can_answer(bot):
    rid = await roster.start_round(bot)
    day = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))[0]
    upd = fake_update(f"r|av|{rid}|{day}", IDS["dad"])
    await roster.on_callback(upd, ctx(bot))
    assert "for the three of you kids" in upd.answers[-1]["text"]
    assert db.scalar("SELECT COUNT(*) FROM roster_availability") == 0


async def test_availability_toggles(bot):
    rid = await roster.start_round(bot)
    day = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))[0]
    await mark_free(bot, rid, "dawn", day)
    assert db.q1("SELECT available FROM roster_availability WHERE duty_date=? AND user_id=?",
                 (day, DAWN))["available"] == 1
    await mark_free(bot, rid, "dawn", day)
    assert db.q1("SELECT available FROM roster_availability WHERE duty_date=? AND user_id=?",
                 (day, DAWN))["available"] == 0


async def test_round_closes_early_once_all_three_kids_answer(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    await mark_free(bot, rid, "mark", dates[0])
    await mark_free(bot, rid, "dawn", dates[1])
    await mark_free(bot, rid, "luke", dates[2])
    assert db.q1("SELECT status FROM roster_rounds WHERE id=?",
                 (rid,))["status"] == "collecting", "still open during the grace period"
    await settle(bot)
    assert db.q1("SELECT status FROM roster_rounds WHERE id=?", (rid,))["status"] == "closed"
    assigned = {s["duty_date"]: s["assignee"] for s in roster.future_slots()}
    assert assigned[dates[0]] == MARK
    assert assigned[dates[1]] == DAWN
    assert assigned[dates[2]] == LUKE
    assert assigned[dates[3]] is None


async def test_closing_pins_the_roster(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    for slug, day in zip(["mark", "dawn", "luke"], dates):
        await mark_free(bot, rid, slug, day)
    await settle(bot)
    assert bot.pinned
    assert db.get_setting("roster_pin_message_id")


async def test_repinning_unpins_the_previous_roster(bot):
    db.set_setting("roster_pin_message_id", 777)
    await roster.announce(bot)
    assert 777 in bot.unpinned


async def test_deadline_closes_the_round_and_assigns(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    await mark_free(bot, rid, "dawn", dates[0])
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_deadline' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["roster_deadline"](bot, job)
    assert db.q1("SELECT assignee FROM roster_slots WHERE duty_date=?",
                 (dates[0],))["assignee"] == DAWN


async def test_unfilled_sundays_are_flagged_with_a_claim_button(bot):
    rid = await roster.start_round(bot)
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_deadline' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["roster_deadline"](bot, job)
    assert bot.said("No cover for")
    assert any(c.startswith("r|take|") for c in bot.callbacks())


async def test_claiming_an_unfilled_sunday_is_race_safe(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_deadline' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["roster_deadline"](bot, job)

    await roster.on_callback(fake_update(f"r|take|{dates[0]}", DAWN), ctx(bot))
    upd = fake_update(f"r|take|{dates[0]}", LUKE)
    await roster.on_callback(upd, ctx(bot))
    assert "already has that one" in upd.answers[-1]["text"]
    assert db.q1("SELECT assignee FROM roster_slots WHERE duty_date=?",
                 (dates[0],))["assignee"] == DAWN
    assert db.duty_counts()[DAWN] == 1


async def test_swap_reopens_the_slot_to_the_others(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    for slug, day in zip(["mark", "dawn", "luke"], dates):
        await mark_free(bot, rid, slug, day)
    await settle(bot)
    bot.reset()

    await roster.on_callback(fake_update(f"r|swap|{dates[0]}", MARK), ctx(bot))
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date=?", (dates[0],))
    assert slot["assignee"] is None and slot["status"] == "open_swap"
    assert db.duty_counts().get(MARK, 0) == 0
    assert bot.said("can't do Sunday duty")

    await roster.on_callback(fake_update(f"r|take|{dates[0]}", LUKE), ctx(bot))
    assert db.q1("SELECT assignee FROM roster_slots WHERE duty_date=?",
                 (dates[0],))["assignee"] == LUKE
    assert db.duty_counts()[LUKE] == 2       # credit follows the replacement


async def test_you_cannot_swap_somebody_elses_sunday(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    for slug, day in zip(["mark", "dawn", "luke"], dates):
        await mark_free(bot, rid, slug, day)
    await settle(bot)
    upd = fake_update(f"r|swap|{dates[0]}", LUKE)
    await roster.on_callback(upd, ctx(bot))
    assert "not your Sunday" in upd.answers[-1]["text"]


async def test_assignment_schedules_both_reminders(bot):
    future = (t.today_local() + timedelta(days=20))
    while future.weekday() != 6:
        future += timedelta(days=1)
    day = future.isoformat()
    roster.ensure_slots([day])
    roster.set_assignee(day, DAWN)
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date=?", (day,))
    tags = sorted(r["payload"] for r in db.q(
        "SELECT payload FROM jobs WHERE kind='roster_remind' AND ref_id=?", (slot["id"],)))
    assert tags == ["sat", "sun"]


async def test_reminder_names_the_assignee(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    for slug, day in zip(["mark", "dawn", "luke"], dates):
        await mark_free(bot, rid, slug, day)
    await settle(bot)
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date=?", (dates[1],))
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_remind' AND ref_id=?", (slot["id"],))
    bot.reset()
    await scheduler.HANDLERS["roster_remind"](bot, job)
    assert "Dawn" in bot.last and "after 6pm" in bot.last


async def test_nag_chases_only_the_silent_kids(bot):
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    await mark_free(bot, rid, "mark", dates[0])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_nag' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["roster_nag"](bot, job)
    assert "Dawn" in bot.last and "Luke" in bot.last and "Mark" not in bot.last


async def test_none_of_these_work_counts_as_answering(bot):
    rid = await roster.start_round(bot)
    await roster.on_callback(fake_update(f"r|none|{rid}", DAWN), ctx(bot))
    await tg.debouncer.flush()
    assert db.scalar("SELECT COUNT(*) FROM roster_responses WHERE round_id=?", (rid,)) == 1
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,))
    assert "Dawn" not in [m["name"] for m in roster.round_state(rnd)["waiting"]]


# --- auto top-up ----------------------------------------------------------

async def test_topup_fires_when_fewer_than_three_sundays_are_covered(bot):
    sundays = [d.isoformat() for d in t.next_sundays(5)]
    roster.ensure_slots(sundays)
    roster.set_assignee(sundays[0], MARK)
    roster.set_assignee(sundays[1], DAWN)
    assert roster.assigned_future_count() == 2

    job = db.q1("SELECT * FROM jobs WHERE kind='roster_topup'") or {"ref_id": None}
    await scheduler.HANDLERS["roster_topup"](bot, job)
    rnd = roster.open_round()
    assert rnd is not None
    # It only asks about the Sundays that still need somebody.
    assert set(roster.round_dates(rnd)) == set(sundays[2:])


async def test_topup_stays_quiet_when_three_are_covered(bot):
    sundays = [d.isoformat() for d in t.next_sundays(5)]
    roster.ensure_slots(sundays)
    for day, uid in zip(sundays[:3], [MARK, DAWN, LUKE]):
        roster.set_assignee(day, uid)
    job = {"ref_id": None}
    await scheduler.HANDLERS["roster_topup"](bot, job)
    assert roster.open_round() is None


async def test_topup_rearms_itself_daily(bot):
    await scheduler.HANDLERS["roster_topup"](bot, {"ref_id": None})
    assert db.has_pending_job("roster_topup")


async def test_topup_waits_for_a_human_to_start_the_roster(bot):
    assert not roster.has_roster()
    await scheduler.HANDLERS["roster_topup"](bot, {"ref_id": None})
    assert roster.open_round() is None


async def test_grace_period_lets_the_last_kid_finish_ticking(bot):
    """Regression: the third kid's first tap used to close the round on them."""
    rid = await roster.start_round(bot)
    dates = roster.round_dates(db.q1("SELECT * FROM roster_rounds WHERE id=?", (rid,)))
    for day in dates:
        await mark_free(bot, rid, "mark", day)
    for day in dates[:2]:
        await mark_free(bot, rid, "dawn", day)

    # Luke now ticks all five, one tap at a time.
    for day in dates:
        await mark_free(bot, rid, "luke", day)
        assert db.q1("SELECT status FROM roster_rounds WHERE id=?",
                     (rid,))["status"] == "collecting"

    await settle(bot)
    free = [r["duty_date"] for r in db.q(
        "SELECT duty_date FROM roster_availability WHERE user_id=? AND available=1",
        (LUKE,))]
    assert sorted(free) == sorted(dates), "all five of Luke's picks were recorded"
    assigned = [s["assignee"] for s in roster.future_slots()]
    assert LUKE in assigned
