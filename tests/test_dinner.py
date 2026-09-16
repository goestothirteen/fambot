"""Feature A acceptance: the full dinner cycle from spec 10."""
from datetime import timedelta


from app import db, dinner, scheduler, tg
from app import timeutil as t
from tests.conftest import IDS, ctx, fake_update


async def vote(bot, poll_id, slug, day):
    upd = fake_update(f"d|v|{poll_id}|{day}", IDS[slug])
    await dinner.on_callback(upd, ctx(bot))
    # Board edits are debounced, so let the render task land before asserting.
    await tg.debouncer.flush()
    return upd


async def everyone_votes(bot, poll_id, day):
    for slug in IDS:
        await vote(bot, poll_id, slug, day)


def days_of(poll_id):
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    return [d.isoformat() for d in dinner.poll_days(poll)]


async def test_poll_opens_on_tomorrow_and_runs_seven_days(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    days = days_of(pid)
    assert len(days) == 7
    assert days[0] == (t.today_local() + timedelta(days=1)).isoformat()
    assert bot.said("FAMILY DINNER VOTE")


async def test_star_appears_only_at_five_of_five(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    assert "⭐" not in bot.board
    assert not dinner.viable_days(db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,)))

    await vote(bot, pid, "luke", day)
    # Everyone has now voted, so the live board becomes the finalize prompt.
    # The unanimous day is still viable and now offered via a finalize button.
    assert dinner.viable_days(db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,))) == [day]
    assert f"d|fin|{pid}|{day}" in bot.callbacks()


async def test_vote_toggles_off_again(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await vote(bot, pid, "dawn", day)
    assert db.q1("SELECT 1 FROM dinner_votes WHERE poll_id=? AND user_id=? AND vote_date=?",
                 (pid, IDS["dawn"], day)) is not None
    upd = await vote(bot, pid, "dawn", day)
    assert db.q1("SELECT 1 FROM dinner_votes WHERE poll_id=? AND user_id=? AND vote_date=?",
                 (pid, IDS["dawn"], day)) is None
    assert "Removed" in upd.answers[-1]["text"]


async def test_no_days_works_counts_as_voted_and_clears_picks(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await vote(bot, pid, "luke", day)
    await dinner.on_callback(fake_update(f"d|none|{pid}", IDS["luke"]), ctx(bot))
    await tg.debouncer.flush()

    poll = db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,))
    st = dinner.poll_state(poll)
    assert "Luke" in st["voted"]
    assert "Luke" not in [m["name"] for m in st["waiting"]]
    assert st["days"][0]["count"] == 0        # the earlier pick was withdrawn


async def test_picking_a_day_undoes_the_none_vote(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await dinner.on_callback(fake_update(f"d|none|{pid}", IDS["luke"]), ctx(bot))
    await vote(bot, pid, "luke", day)
    assert db.q1("SELECT 1 FROM dinner_votes WHERE poll_id=? AND user_id=? "
                 "AND vote_date='NONE'", (pid, IDS["luke"])) is None


async def test_nag_mentions_only_non_voters(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await vote(bot, pid, "dad", day)
    await vote(bot, pid, "mom", day)
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    await scheduler.HANDLERS["dinner_nag"](bot, job)
    nag = bot.last
    assert "Mark" in nag and "Dawn" in nag and "Luke" in nag
    assert "Dad" not in nag and "Mom" not in nag


async def test_nag_rearms_itself(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    db.finish_job(job["id"])
    await scheduler.HANDLERS["dinner_nag"](bot, job)
    assert db.has_pending_job("dinner_nag", pid)


async def test_nag_stops_once_everyone_voted(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    await everyone_votes(bot, pid, days_of(pid)[0])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    await scheduler.HANDLERS["dinner_nag"](bot, job)
    assert not bot.said("Waiting on")


async def test_lock_closes_the_poll_and_schedules_reminders(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    assert await dinner.lock(bot, pid, day, IDS["mark"])

    poll = db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,))
    assert poll["status"] == "locked" and poll["locked_date"] == day
    assert not db.has_pending_job("dinner_nag", pid)
    assert not db.has_pending_job("dinner_deadline", pid)

    event = dinner.upcoming_event()
    assert event["dinner_date"] == day
    kinds = [r["payload"] for r in db.q(
        "SELECT payload FROM jobs WHERE kind='dinner_remind' AND ref_id=? AND done=0",
        (event["id"],))]
    # Dinner is tomorrow, so T-3 and T-1 are already in the past; only day-of stands.
    assert "day" in kinds
    assert db.has_pending_job("dinner_done", event["id"])


async def test_lock_is_race_safe(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    days = days_of(pid)
    await everyone_votes(bot, pid, days[0])
    await everyone_votes(bot, pid, days[1])
    assert await dinner.lock(bot, pid, days[0], IDS["mark"]) is True
    assert await dinner.lock(bot, pid, days[1], IDS["dawn"]) is False
    assert db.scalar("SELECT COUNT(*) FROM dinner_events") == 1


async def test_far_off_dinner_gets_all_three_reminders(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[5]              # six days out
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()
    tags = sorted(r["payload"] for r in db.q(
        "SELECT payload FROM jobs WHERE kind='dinner_remind' AND ref_id=?", (event["id"],)))
    assert tags == ["day", "t1", "t3"]


async def test_deadline_with_viable_days_offers_locks_and_stops_nagging(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert bot.said("Time's up")
    assert not db.has_pending_job("dinner_nag", pid)
    assert db.q1("SELECT status FROM dinner_polls WHERE id=?", (pid,))["status"] == "open"


async def test_deadline_with_no_viable_day_offers_extend(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    await vote(bot, pid, "dawn", days_of(pid)[0])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert bot.said("No day worked")
    assert db.q1("SELECT status FROM dinner_polls WHERE id=?", (pid,))["status"] == "expired"
    assert f"d|ext|{pid}" in bot.callbacks()


async def test_extend_rolls_the_window_forward_and_resets_votes(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    old_days = days_of(pid)
    await vote(bot, pid, "dawn", old_days[0])
    await dinner.on_callback(fake_update(f"d|ext|{pid}", IDS["mark"]), ctx(bot))

    new_poll = dinner.open_poll()
    assert new_poll["id"] != pid
    from datetime import timedelta
    expected = (t.parse_date(old_days[-1]) + timedelta(days=1)).isoformat()
    assert days_of(new_poll["id"])[0] == expected
    assert db.scalar("SELECT COUNT(*) FROM dinner_votes WHERE poll_id=?",
                     (new_poll["id"],)) == 0


async def test_dropout_announces_and_offers_revote(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()
    bot.reset()

    await dinner.on_callback(fake_update(f"d|out|{event['id']}", IDS["luke"]), ctx(bot))
    assert bot.said("Luke can't make dinner")
    assert f"d|revote|{event['id']}" in bot.callbacks()


async def test_revote_cancels_the_dinner_and_opens_a_fresh_poll(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()

    await dinner.on_callback(fake_update(f"d|revote|{event['id']}", IDS["luke"]), ctx(bot))
    assert db.q1("SELECT status FROM dinner_events WHERE id=?",
                 (event["id"],))["status"] == "cancelled"
    assert not db.has_pending_job("dinner_remind", event["id"])
    assert dinner.open_poll() is not None
    assert dinner.upcoming_event() is None


async def test_coming_back_in_clears_the_dropout(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()
    await dinner.on_callback(fake_update(f"d|out|{event['id']}", IDS["luke"]), ctx(bot))
    await dinner.on_callback(fake_update(f"d|in|{event['id']}", IDS["luke"]), ctx(bot))
    assert db.scalar("SELECT COUNT(*) FROM dinner_dropouts WHERE event_id=?",
                     (event["id"],)) == 0


async def test_only_one_poll_at_a_time(bot):
    await dinner.open_new(bot, IDS["mark"])
    await dinner.on_callback(fake_update("d|open", IDS["dawn"]), ctx(bot))
    assert db.scalar("SELECT COUNT(*) FROM dinner_polls WHERE status='open'") == 1


async def test_unregistered_member_blocks_five_of_five(bot, unregister_all):
    unregister_all("luke")
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    for slug in ["dad", "mom", "mark"]:
        await vote(bot, pid, slug, day)
    # Still someone to hear from, so the vote board shows and warns about 5/5.
    assert "⭐" not in bot.board
    assert "Not registered" in bot.board
    # Nobody is ever "viable" because required counts the unregistered member.
    assert not dinner.viable_days(db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,)))


async def test_unregistered_tapper_is_told_to_register(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    upd = fake_update(f"d|v|{pid}|{days_of(pid)[0]}", 999999)
    await dinner.on_callback(upd, ctx(bot))
    assert "don't know who you are" in upd.answers[-1]["text"]


async def test_board_reposts_when_the_message_is_gone(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    old_id = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    bot.fail_edit = True
    await dinner.render(bot, pid)
    new_id = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    assert new_id != old_id


async def test_dinner_done_marks_the_event_complete(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_done' AND ref_id=?", (event["id"],))
    await scheduler.HANDLERS["dinner_done"](bot, job)
    assert db.q1("SELECT status FROM dinner_events WHERE id=?",
                 (event["id"],))["status"] == "done"


# --- one live board at a time (stale-box regression) -----------------------

async def test_lock_confirm_retires_the_vote_board(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    board_id = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]

    await dinner.on_callback(fake_update(f"d|lock|{pid}|{day}", IDS["mark"]), ctx(bot))

    # The old vote board is deleted and its id forgotten, so no stale box remains.
    assert board_id in bot.deleted
    assert db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"] is None


async def test_deadline_board_retires_the_vote_board(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    await everyone_votes(bot, pid, days_of(pid)[0])
    board_id = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert board_id in bot.deleted
    assert db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"] is None


async def test_extend_retires_old_board_leaving_only_the_new_poll(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    await vote(bot, pid, "dawn", days_of(pid)[0])
    old_board = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)   # expires -> offers extend
    bot.reset()

    await dinner.on_callback(fake_update(f"d|ext|{pid}", IDS["mark"]), ctx(bot))

    new_poll = dinner.open_poll()
    assert new_poll["id"] != pid
    # Exactly one live board: the old one is gone, only the fresh poll stands.
    assert db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"] is None
    assert new_poll["message_id"] is not None


async def test_cancel_vote_retires_the_board(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    board_id = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    await dinner.on_callback(fake_update(f"d|kill|{pid}", IDS["mark"]), ctx(bot))
    assert board_id in bot.deleted
    assert db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"] is None


async def test_retire_survives_an_already_deleted_board(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    bot.fail_delete = True          # user already removed the message
    # Must not raise, and must still forget the id.
    await dinner.retire_poll_board(bot, pid)
    assert db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"] is None


# --- "Finalize the date" once everyone has voted ---------------------------

async def vote_none(bot, poll_id, slug):
    await dinner.on_callback(fake_update(f"d|none|{poll_id}", IDS[slug]), ctx(bot))
    await tg.debouncer.flush()


def _poll(pid):
    return db.q1("SELECT * FROM dinner_polls WHERE id=?", (pid,))


async def test_all_voted_detects_when_everyone_has_weighed_in(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    days = days_of(pid)
    assert dinner.all_voted(_poll(pid)) is False
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, days[0])
    assert dinner.all_voted(_poll(pid)) is False   # Luke still hasn't voted
    # A NONE vote counts just like picking a day.
    await vote_none(bot, pid, "luke")
    assert dinner.all_voted(_poll(pid)) is True


async def test_finalize_prompt_lists_days_with_votes_ranked_by_count(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    days = days_of(pid)
    # day0: 4 votes; day1: 2 votes; day2: 0 votes. Luke picks nothing but NONE.
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, days[0])
    for slug in ["dad", "mom"]:
        await vote(bot, pid, slug, days[1])
    await vote_none(bot, pid, "luke")

    ranked = dinner.ranked_days(_poll(pid))
    assert [d["date"] for d in ranked] == [days[0], days[1]]   # best-first, day2 dropped
    assert ranked[0]["count"] == 4 and ranked[1]["count"] == 2

    board = bot.board
    assert "Everyone's voted" in board
    # Both voted days appear as finalize buttons; the empty day does not.
    cbs = bot.callbacks()
    assert f"d|fin|{pid}|{days[0]}" in cbs
    assert f"d|fin|{pid}|{days[1]}" in cbs
    assert f"d|fin|{pid}|{days[2]}" not in cbs


async def test_non_admin_cannot_finalize(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    await vote_none(bot, pid, "luke")   # everyone voted; day is 4/5

    upd = fake_update(f"d|fin|{pid}|{day}", IDS["dawn"])   # Dawn is not admin
    await dinner.on_callback(upd, ctx(bot))
    assert "Only Mark can finalize" in upd.answers[-1]["text"]
    assert upd.answers[-1]["alert"] is True
    assert _poll(pid)["status"] == "open"           # nothing locked
    assert db.scalar("SELECT COUNT(*) FROM dinner_events") == 0


async def test_admin_finalizes_a_four_of_five_day_and_schedules_reminders(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[5]           # six days out, so all three reminders stand
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    await vote_none(bot, pid, "luke")   # 4/5, non-unanimous

    upd = fake_update(f"d|fin|{pid}|{day}", IDS["mark"])   # Mark is admin
    await dinner.on_callback(upd, ctx(bot))

    poll = _poll(pid)
    assert poll["status"] == "locked" and poll["locked_date"] == day
    event = dinner.upcoming_event()
    assert event["dinner_date"] == day
    tags = sorted(r["payload"] for r in db.q(
        "SELECT payload FROM jobs WHERE kind='dinner_remind' AND ref_id=?", (event["id"],)))
    assert tags == ["day", "t1", "t3"]
    assert db.has_pending_job("dinner_done", event["id"])
    assert not db.has_pending_job("dinner_nag", pid)


async def test_admin_finalize_is_race_safe(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    days = days_of(pid)
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, days[0])
    await vote(bot, pid, "dad", days[1])
    await vote_none(bot, pid, "luke")
    assert await dinner.lock(bot, pid, days[0], IDS["mark"]) is True
    # A second finalize on the now-locked poll must not double-book.
    upd = fake_update(f"d|fin|{pid}|{days[1]}", IDS["mark"])
    await dinner.on_callback(upd, ctx(bot))
    assert db.scalar("SELECT COUNT(*) FROM dinner_events") == 1


async def test_all_none_shows_nobody_can_make_any_day(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    for slug in IDS:
        await vote_none(bot, pid, slug)
    assert dinner.all_voted(_poll(pid)) is True
    assert dinner.ranked_days(_poll(pid)) == []
    assert bot.said("nobody can make any day")
    # No finalize buttons when there is no real day to pick.
    assert not any(c.startswith(f"d|fin|{pid}") for c in bot.callbacks())


async def test_finalize_keeps_one_live_board(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    board_before = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    await vote_none(bot, pid, "luke")   # flips the board to the finalize prompt
    # Same tracked message id: the vote board became the finalize prompt in place.
    board_after = db.q1("SELECT message_id FROM dinner_polls WHERE id=?", (pid,))["message_id"]
    assert board_after == board_before
    assert bot.edits and "Everyone's voted" in bot.edits[-1].text
