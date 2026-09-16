"""One realistic week, start to finish - the spec 10 checklist as a story."""
from datetime import timedelta

from app import boards, db, dinner, helpreq, main, roster, scheduler, tg
from app import timeutil as t
from tests.conftest import GROUP, IDS, ctx, fake_update, settle
from tests.test_handlers import text_update


async def tap(bot, data, who):
    upd = fake_update(data, IDS[who] if isinstance(who, str) else who)
    for handler, prefix in ((dinner.on_callback, "d|"), (helpreq.on_callback, "h|"),
                            (roster.on_callback, "r|"), (main.on_setup_cb, "s|")):
        if data.startswith(prefix):
            await handler(upd, ctx(bot))
            break
    await tg.debouncer.flush()
    return upd


async def test_a_week_in_the_life(bot, unregister_all):
    # --- Monday: Mark sets the bot up, the family registers -----------------
    unregister_all("dad", "mom", "mark", "dawn", "luke")
    db.x("DELETE FROM settings WHERE key='group_chat_id'")

    upd = text_update("/setup")
    replies = []
    async def reply_text(text, reply_markup=None, **kw):
        replies.append((text, reply_markup))
        from types import SimpleNamespace
        return SimpleNamespace(message_id=1)
    upd.effective_message.reply_text = reply_text
    await main.cmd_setup(upd, ctx(bot))

    assert db.group_chat_id() == GROUP
    assert replies[0][1] is not None, "persistent keyboard went out"

    for i, slug in enumerate(["dad", "mom", "mark", "dawn", "luke"]):
        await tap(bot, f"s|reg|{slug}", 200 + i)
    assert len(db.registered()) == 5
    ids = {m["slug"]: m["user_id"] for m in db.members()}

    # --- Tuesday: normal chatter is ignored ---------------------------------
    bot.reset()
    await main.on_text(text_update("anyone free to walk rush tmr?"), ctx(bot))
    await main.on_text(text_update("haha ok"), ctx(bot))
    assert bot.sent == [], "the bot stayed out of ordinary conversation"

    # --- Tuesday: Mom starts a dinner vote from the keyboard ----------------
    await main.on_text(text_update(boards.L_DINNER, user_id=ids["mom"]), ctx(bot))
    assert bot.said("Start a dinner vote")
    await tap(bot, "d|open", ids["mom"])
    poll = dinner.open_poll()
    days = [d.isoformat() for d in dinner.poll_days(poll)]

    # Four vote for Friday; no star yet.
    for slug in ["dad", "mom", "mark", "dawn"]:
        await tap(bot, f"d|v|{poll['id']}|{days[3]}", ids[slug])
    assert "⭐" not in bot.board

    # Luke gets nagged, then votes.
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_nag' AND done=0")
    bot.reset()
    await scheduler.HANDLERS["dinner_nag"](bot, job)
    assert "Luke" in bot.last and "Dawn" not in bot.last

    await tap(bot, f"d|v|{poll['id']}|{days[3]}", ids["luke"])
    # Everyone's voted, so the board becomes the admin-only finalize prompt.
    assert bot.said("Everyone's voted")
    assert f"d|fin|{poll['id']}|{days[3]}" in bot.callbacks()

    # A non-admin can't finalize.
    await tap(bot, f"d|fin|{poll['id']}|{days[3]}", ids["dawn"])
    assert dinner.open_poll() is not None            # still open, nothing locked

    # Mark (admin) finalizes the unanimous day.
    await tap(bot, f"d|fin|{poll['id']}|{days[3]}", ids["mark"])
    event = dinner.upcoming_event()
    assert event["dinner_date"] == days[3]
    assert dinner.open_poll() is None

    # --- Wednesday: Dad needs Rush covered ----------------------------------
    tomorrow = (t.today_local() + timedelta(days=1)).isoformat()
    await tap(bot, "h|new|dog", ids["dad"])
    await tap(bot, f"h|d|dog|{tomorrow}", ids["dad"])
    await tap(bot, f"h|w|dog|{tomorrow}|evening", ids["dad"])
    await tap(bot, f"h|mk|dog_takeover|{tomorrow}|evening", ids["dad"])
    req = helpreq.open_requests()[0]

    # Dawn cannot drive, so the button refuses her.
    upd = await tap(bot, f"h|claim|{req['id']}", ids["dawn"])
    assert "needs a driver" in upd.answers[-1]["text"]
    # Mark can.
    await tap(bot, f"h|claim|{req['id']}", ids["mark"])
    assert db.q1("SELECT claimed_by FROM help_requests WHERE id=?",
                 (req["id"],))["claimed_by"] == ids["mark"]

    # --- Wednesday: Mom needs someone home on Sunday ------------------------
    sunday = t.next_sundays(1)[0].isoformat()
    await tap(bot, "h|new|cov", ids["mom"])
    await tap(bot, f"h|d|cov|{sunday}", ids["mom"])
    await tap(bot, f"h|w|cov|{sunday}|evening", ids["mom"])
    await tap(bot, f"h|mk|coverage|{sunday}|evening|dogs", ids["mom"])
    cov = helpreq.open_requests()[0]
    await tap(bot, f"h|claim|{cov['id']}", ids["luke"])
    assert db.duty_counts()[ids["luke"]] == 1

    # --- Thursday: the Sunday roster gets set up ----------------------------
    await tap(bot, "r|setup", ids["mark"])
    rnd = roster.open_round()
    sundays = roster.round_dates(rnd)
    assert len(sundays) == 5

    # All three kids answer, so the round closes early.
    for s in sundays:
        await tap(bot, f"r|av|{rnd['id']}|{s}", ids["mark"])
    for s in sundays[:3]:
        await tap(bot, f"r|av|{rnd['id']}|{s}", ids["dawn"])
    for s in sundays:
        await tap(bot, f"r|av|{rnd['id']}|{s}", ids["luke"])
    await settle(bot)

    assert roster.open_round() is None, "round closed once everyone answered"
    slots = {s["duty_date"]: s["assignee"] for s in roster.future_slots()}
    assert all(v is not None for v in slots.values()), "every Sunday got somebody"
    assert bot.pinned, "the roster was pinned"

    # Luke already did a coverage duty, so he should not be the busiest.
    counts = db.duty_counts()
    assert max(counts.values()) - min(counts.values()) <= 2, \
        f"duties are spread reasonably: {counts}"

    # --- Friday: Dawn swaps her Sunday out ----------------------------------
    dawn_day = next(d for d, a in slots.items() if a == ids["dawn"])
    await tap(bot, f"r|swap|{dawn_day}", ids["dawn"])
    assert db.q1("SELECT assignee FROM roster_slots WHERE duty_date=?",
                 (dawn_day,))["assignee"] is None
    await tap(bot, f"r|take|{dawn_day}", ids["mark"])
    assert db.q1("SELECT assignee FROM roster_slots WHERE duty_date=?",
                 (dawn_day,))["assignee"] == ids["mark"]

    # --- Saturday: the box reboots ------------------------------------------
    path = db.conn().execute("PRAGMA database_list").fetchone()[2]
    db.close()
    db.init(path)

    assert dinner.upcoming_event()["dinner_date"] == days[3]
    assert db.q1("SELECT claimed_by FROM help_requests WHERE id=?",
                 (req["id"],))["claimed_by"] == ids["mark"]
    assert roster.assigned_future_count() >= 3
    assert db.has_pending_job("roster_topup")
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE done=0") > 0

    # --- Saturday: Luke drops out of dinner, the family re-votes ------------
    bot.reset()
    await tap(bot, f"d|out|{event['id']}", ids["luke"])
    assert bot.said("Luke can't make dinner")
    await tap(bot, f"d|revote|{event['id']}", ids["luke"])
    assert dinner.upcoming_event() is None
    assert dinner.open_poll() is not None


async def test_the_bot_never_assigns_a_duty_nobody_volunteered_for(bot):
    """Spec 2.2: the bot surfaces options, humans decide."""
    rid = await roster.start_round(bot)
    job = db.q1("SELECT * FROM jobs WHERE kind='roster_deadline' AND ref_id=?", (rid,))
    await scheduler.HANDLERS["roster_deadline"](bot, job)
    assert all(s["assignee"] is None for s in roster.future_slots())
    assert bot.said("No cover for")


async def test_the_bot_never_auto_locks_a_dinner(bot):
    """Even at 5/5 and past the deadline, a human must tap Lock."""
    pid = await dinner.open_new(bot, IDS["mark"])
    day = dinner.poll_days(db.q1("SELECT * FROM dinner_polls WHERE id=?",
                                 (pid,)))[0].isoformat()
    for uid in IDS.values():
        db.x("INSERT OR IGNORE INTO dinner_votes (poll_id, user_id, vote_date) "
             "VALUES (?, ?, ?)", (pid, uid, day))
    job = db.q1("SELECT * FROM jobs WHERE kind='dinner_deadline'")
    await scheduler.HANDLERS["dinner_deadline"](bot, job)
    assert dinner.upcoming_event() is None, "no dinner was booked without a tap"
    assert any(c.startswith("d|lock|") for c in bot.callbacks())
