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


async def test_the_nag_moves_the_board_instead_of_adding_a_message(bot):
    """Chasing non-voters must not cost the family group a message."""
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await vote(bot, pid, "dad", day)
    await vote(bot, pid, "mom", day)
    old_board = _poll(pid)["message_id"]
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    await scheduler.HANDLERS["dinner_nag"](bot, job)

    # One message out, one in: the board itself is the chase-up.
    assert len(bot.sent) == 1
    assert old_board in bot.deleted
    assert _poll(pid)["message_id"] == bot.sent[-1].message_id
    # Only the people still owing a vote get a ping they can feel.
    for slug in ("mark", "dawn", "luke"):
        assert f"tg://user?id={IDS[slug]}" in bot.last
    for slug in ("dad", "mom"):
        assert f"tg://user?id={IDS[slug]}" not in bot.last


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


async def test_deadline_hands_the_day_to_the_admin_and_stops_nagging(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert bot.said("Time's up")
    assert f"d|fin|{pid}|{day}" in bot.callbacks()
    assert not db.has_pending_job("dinner_nag", pid)
    assert db.q1("SELECT status FROM dinner_polls WHERE id=?", (pid,))["status"] == "open"


async def test_four_yeses_and_a_no_is_a_dinner_not_a_dead_end(bot):
    """The old build called this "no day worked for all five" and gave up."""
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert bot.said("Time's up")
    assert f"d|fin|{pid}|{day}" in bot.callbacks()

    await dinner.on_callback(fake_update(f"d|fin|{pid}|{day}", IDS["mark"]), ctx(bot))
    assert dinner.upcoming_event()["dinner_date"] == day


async def test_deadline_with_nobody_voting_offers_the_next_week(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    bot.reset()
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert bot.said("nobody picked a day")
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


async def test_reminders_never_ask_anyone_to_re_confirm(bot):
    """The guest list was settled by the vote; only the admin changes the plan."""
    event = await _locked_four_of_five(bot)
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_remind' AND payload='t1'")
    await scheduler.HANDLERS["dinner_remind"](bot, job)

    assert "tomorrow" in bot.last
    assert bot.callbacks() == [f"d|revote|{event['id']}", f"d|ecancel|{event['id']}"]


async def test_reminders_ping_exactly_the_people_who_said_they_could_come(bot):
    await _locked_four_of_five(bot)
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_remind' AND payload='t1'")
    await scheduler.HANDLERS["dinner_remind"](bot, job)

    for slug in ("dad", "mom", "mark", "dawn"):
        assert f"tg://user?id={IDS[slug]}" in bot.last
    assert f"tg://user?id={IDS['luke']}" not in bot.last
    assert "Not coming: Luke" in bot.last


async def test_each_reminder_moves_the_one_card_rather_than_stacking_up(bot):
    await _locked_four_of_five(bot)
    bot.reset()

    for tag in ("t3", "t1", "day"):
        job = db.q1("SELECT * FROM jobs WHERE kind='dinner_remind' AND payload=?", (tag,))
        await scheduler.HANDLERS["dinner_remind"](bot, job)

    # Three reminders, three deletes: the chat never holds more than one card.
    assert len(bot.sent) == 3
    assert len(bot.deleted) == 3
    assert "tonight" in bot.last


async def test_revote_cancels_the_dinner_and_opens_a_fresh_poll(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()

    await dinner.on_callback(fake_update(f"d|revote|{event['id']}", IDS["mark"]), ctx(bot))
    assert db.q1("SELECT status FROM dinner_events WHERE id=?",
                 (event["id"],))["status"] == "cancelled"
    assert not db.has_pending_job("dinner_remind", event["id"])
    assert dinner.open_poll() is not None
    assert dinner.upcoming_event() is None


async def test_only_the_admin_can_cancel_or_move_a_locked_dinner(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()

    upd = fake_update(f"d|ecancel|{event['id']}", IDS["luke"])
    await dinner.on_callback(upd, ctx(bot))
    assert "Only Mark" in upd.answers[-1]["text"]
    assert dinner.upcoming_event() is not None


async def test_the_admin_cancels_into_the_same_box_with_a_way_back(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    event = dinner.upcoming_event()
    card_id = event["message_id"]
    bot.reset()

    await dinner.on_callback(fake_update(f"d|ecancel|{event['id']}", IDS["mark"]), ctx(bot))

    assert bot.sent == [], "cancelling is a quiet edit, not an announcement"
    assert bot.edits[-1].message_id == card_id
    assert "is off" in bot.edits[-1].text
    assert "d|open" in bot.callbacks()
    assert not db.has_pending_job("dinner_remind", event["id"])


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

async def test_lock_confirm_happens_inside_the_same_box(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    board_id = _poll(pid)["message_id"]
    bot.reset()

    await dinner.on_callback(fake_update(f"d|lock|{pid}|{day}", IDS["mark"]), ctx(bot))

    # Nothing posted, nothing deleted - the board just changes what it says.
    assert bot.sent == []
    assert bot.deleted == []
    assert _poll(pid)["message_id"] == board_id
    assert "Lock dinner in for" in bot.edits[-1].text


async def test_the_deadline_moves_the_box_rather_than_adding_one(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    await everyone_votes(bot, pid, days_of(pid)[0])
    board_id = _poll(pid)["message_id"]
    bot.reset()

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)

    assert board_id in bot.deleted
    assert len(bot.sent) == 1
    assert _poll(pid)["message_id"] == bot.sent[-1].message_id


async def test_dinner_again_moves_the_one_board_instead_of_posting_another(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    board_id = _poll(pid)["message_id"]
    bot.reset()

    await dinner.entry(bot, IDS["dawn"])

    assert dinner.open_poll()["id"] == pid, "no second vote was started"
    assert board_id in bot.deleted
    assert len(bot.sent) == 1
    assert _poll(pid)["message_id"] == bot.sent[-1].message_id


async def test_dinner_on_a_locked_night_moves_the_card_instead_of_repeating_it(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[0]
    await everyone_votes(bot, pid, day)
    await dinner.lock(bot, pid, day, IDS["mark"])
    bot.reset()

    await dinner.entry(bot, IDS["dawn"])
    await dinner.entry(bot, IDS["luke"])

    # Two people asked; the chat still holds exactly one dinner card.
    assert len(bot.sent) == 2
    assert len(bot.deleted) == 2
    assert db.scalar("SELECT COUNT(*) FROM dinner_events") == 1


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


async def test_cancelling_the_vote_empties_the_box_without_a_new_message(bot):
    pid = await dinner.open_new(bot, IDS["mark"])
    board_id = _poll(pid)["message_id"]
    bot.reset()

    await dinner.on_callback(fake_update(f"d|kill|{pid}", IDS["mark"]), ctx(bot))

    assert bot.sent == []
    assert bot.edits[-1].message_id == board_id
    assert "Dinner vote cancelled" in bot.edits[-1].text


async def test_a_board_too_old_to_delete_is_defused_not_left_live(bot):
    """Without group-admin rights Telegram refuses deletes after 48 hours.

    The fallback must never leave two tappable boards in the chat.
    """
    pid = await dinner.open_new(bot, IDS["mark"])
    board_id = _poll(pid)["message_id"]
    bot.fail_delete = True
    bot.reset()

    await dinner.render(bot, pid, bump=True)

    assert len(bot.sent) == 1
    assert _poll(pid)["message_id"] == bot.sent[-1].message_id
    stale = [e for e in bot.edits if e.message_id == board_id][-1]
    assert "moved to the bottom" in stale.text
    assert stale.markup is None, "the old board keeps no live buttons"


# --- "Finalize the date" once everyone has voted ---------------------------

async def vote_none(bot, poll_id, slug):
    await dinner.on_callback(fake_update(f"d|none|{poll_id}", IDS[slug]), ctx(bot))
    await tg.debouncer.flush()


async def test_a_whole_dinner_only_ever_occupies_one_message(bot):
    """The point of the rework: the family group never fills up with dinner.

    Vote, chase-up, finalize, three reminders and the wrap-up - and at every
    single step there is exactly one live dinner message in the chat.
    """
    def live_boxes():
        return len([m.message_id for m in bot.sent if m.message_id not in bot.deleted])

    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[5]
    assert live_boxes() == 1

    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    assert live_boxes() == 1, "voting is silent"

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    await scheduler.HANDLERS["dinner_nag"](bot, job)
    assert live_boxes() == 1, "the chase-up replaced the board"

    await vote_none(bot, pid, "luke")
    await dinner.on_callback(fake_update(f"d|fin|{pid}|{day}", IDS["mark"]), ctx(bot))
    event = dinner.upcoming_event()
    assert live_boxes() == 1, "the vote box became the dinner card"

    for tag in ("t3", "t1", "day"):
        job = db.q1("SELECT * FROM jobs WHERE kind='dinner_remind' AND payload=?", (tag,))
        await scheduler.HANDLERS["dinner_remind"](bot, job)
        assert live_boxes() == 1, f"the {tag} reminder moved the card"

    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_done' AND ref_id=?", (event["id"],))
    await scheduler.HANDLERS["dinner_done"](bot, job)
    assert live_boxes() == 1, "the wrap-up is a quiet edit"


async def _locked_four_of_five(bot):
    """Four can make a day six days out, Luke cannot, and the admin locks it."""
    pid = await dinner.open_new(bot, IDS["mark"])
    day = days_of(pid)[5]           # far enough out for all three reminders
    for slug in ["dad", "mom", "mark", "dawn"]:
        await vote(bot, pid, slug, day)
    await vote_none(bot, pid, "luke")
    await dinner.on_callback(fake_update(f"d|fin|{pid}|{day}", IDS["mark"]), ctx(bot))
    return dinner.upcoming_event()


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
    assert "Only Mark can change the dinner" in upd.answers[-1]["text"]
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
