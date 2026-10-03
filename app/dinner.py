"""Feature A - the dinner scheduler.

The engine: open a poll over a run of days (the next 7, or any first-to-last
range picked on buttons), everyone multi-selects, a day becomes viable only
when all five have ticked it, and then a human locks it in. The bot never
picks the day itself (spec 2.2).

One box, always. A dinner occupies exactly one message in the family group
from the moment a vote opens until the night itself: the vote board, the lock
confirmation, the "dinner is on" card and every reminder are all the same
message being rewritten. Editing is silent, so when something genuinely has
to reach phones the box is taken down and re-posted at the bottom instead -
still one message, but one that pings. Nothing else is ever posted, because
the family group is for the family, not for the bot.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app import boards, db, scheduler, tg
from app import timeutil as t

log = logging.getLogger(__name__)

NONE = "NONE"   # sentinel vote meaning "no day works for me"


# --- queries ---------------------------------------------------------------

def open_poll():
    return db.q1("SELECT * FROM dinner_polls WHERE status = 'open' "
                 "ORDER BY id DESC LIMIT 1")


def upcoming_event():
    return db.q1("SELECT * FROM dinner_events WHERE status = 'upcoming' "
                 "ORDER BY dinner_date LIMIT 1")


def poll_days(poll) -> list[date]:
    start = t.parse_date(poll["start_date"])
    return [start + timedelta(days=i) for i in range(poll["days"])]


def _votes(poll_id: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in db.q("SELECT user_id, vote_date FROM dinner_votes WHERE poll_id = ?",
                  (poll_id,)):
        out.setdefault(r["vote_date"], []).append(r["user_id"])
    return out


def poll_state(poll) -> dict:
    """Everything the board needs, computed fresh from the DB."""
    everyone = db.members()
    required = len(everyone)
    votes = _votes(poll["id"])
    voters = {uid for uids in votes.values() for uid in uids}

    days = []
    for dd in poll_days(poll):
        key = dd.isoformat()
        uids = votes.get(key, [])
        days.append({
            "date": key,
            "count": len(uids),
            "required": required,
            "names": [db.name_of(u) for u in uids],
            # All five must be free, and everyone must actually be registered,
            # or "5/5" would be a lie.
            "viable": len(uids) >= required and required > 0,
        })
    return {
        "days": days,
        "required": required,
        "voted": [m["name"] for m in everyone if m["user_id"] in voters],
        "waiting": [m for m in everyone
                    if m["user_id"] is not None and m["user_id"] not in voters],
        "missing": [m["name"] for m in db.unregistered()],
    }


def viable_days(poll) -> list[str]:
    return [d["date"] for d in poll_state(poll)["days"] if d["viable"]]


def all_voted(poll) -> bool:
    """True once every registered member has cast at least one vote.

    poll_state["waiting"] is exactly the registered members with no vote yet
    (a real day OR the NONE sentinel both count as voting). Empty waiting means
    everyone has weighed in, so we can finalize.
    """
    return not poll_state(poll)["waiting"]


def past_deadline(poll) -> bool:
    return t.parse_iso(poll["deadline_at"]) <= t.now_utc()


def ranked_days(poll) -> list[dict]:
    """Candidate days with >=1 real vote, best-first (most people who can make it).

    Ties break on the earlier date so the ordering is stable. Excludes the NONE
    sentinel (it isn't a real day and never appears in poll_state days).
    """
    days = [d for d in poll_state(poll)["days"] if d["count"] > 0]
    return sorted(days, key=lambda d: (-d["count"], d["date"]))


def _admin_name() -> str:
    for m in db.members():
        if m["is_admin"]:
            return m["name"]
    return "the admin"


# --- the guest list --------------------------------------------------------

def _snapshot_attendees(event_id: int, poll_id: int, day: str) -> None:
    """Freeze who is coming, at the moment the day is locked in.

    Whoever ticked the chosen day is expected at dinner. Taking a copy means
    a later poll, or someone editing votes on a stale board, cannot quietly
    change who the bot thinks is coming.
    """
    for r in db.q("SELECT user_id FROM dinner_votes WHERE poll_id = ? AND vote_date = ?",
                  (poll_id, day)):
        db.x("INSERT OR IGNORE INTO dinner_attendees (event_id, user_id) "
             "VALUES (?, ?)", (event_id, r["user_id"]))


def attendees(event_id: int) -> list:
    ids = {r["user_id"] for r in
           db.q("SELECT user_id FROM dinner_attendees WHERE event_id = ?", (event_id,))}
    return [m for m in db.members() if m["user_id"] in ids]


def absentees(event_id: int) -> list[str]:
    ids = {r["user_id"] for r in
           db.q("SELECT user_id FROM dinner_attendees WHERE event_id = ?", (event_id,))}
    return [m["name"] for m in db.members() if m["user_id"] not in ids]


# --- the one box -----------------------------------------------------------

async def _show(bot: Bot, message_id: int | None, text, markup,
                bump: bool) -> int | None:
    """Put `text` in the dinner box, and tell the caller where the box now is.

    bump=False edits in place: instant, and nobody's phone lights up. bump=True
    takes the box down and re-posts it at the bottom, which is the only way a
    reminder or a chase-up actually reaches anyone.
    """
    chat_id = db.group_chat_id()
    if chat_id is None:
        return None
    if bump:
        return await tg.repost(bot, chat_id, message_id, text, markup,
                               boards.dinner_board_moved())
    return await tg.edit_or_repost(bot, chat_id, message_id, text, markup)


async def _show_poll(bot: Bot, poll_id: int, text, markup, bump: bool) -> None:
    poll = db.q1("SELECT message_id FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None:
        return
    new_id = await _show(bot, poll["message_id"], text, markup, bump)
    if new_id and new_id != poll["message_id"]:
        db.x("UPDATE dinner_polls SET message_id = ? WHERE id = ?", (new_id, poll_id))


async def _show_event(bot: Bot, event_id: int, text, markup, bump: bool) -> None:
    event = db.q1("SELECT message_id FROM dinner_events WHERE id = ?", (event_id,))
    if event is None:
        return
    new_id = await _show(bot, event["message_id"], text, markup, bump)
    if new_id and new_id != event["message_id"]:
        db.x("UPDATE dinner_events SET message_id = ? WHERE id = ?", (new_id, event_id))


# --- rendering -------------------------------------------------------------

async def render(bot: Bot, poll_id: int, bump: bool = False) -> None:
    """Draw the vote board, or the pick-a-day prompt once voting is over.

    Voting is over when everyone has answered *or* the deadline has passed.
    Either way the admin can take any day with at least one vote - "four of us
    can do Friday" is a dinner, not a failure - so a day nobody can all make
    no longer dead-ends the whole thing.
    """
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None or poll["status"] != "open":
        return
    st = poll_state(poll)
    closed = past_deadline(poll)
    if closed or not st["waiting"]:
        ranked = ranked_days(poll)
        if ranked:
            text, markup = boards.dinner_finalize(poll["id"], ranked,
                                                  _admin_name(), closed)
        else:
            text, markup = boards.dinner_finalize_none(poll["id"], poll["days"], closed)
    else:
        # Mentions, not names: a re-posted board is how non-voters get chased,
        # so the waiting list has to be tappable pings.
        text, markup = boards.dinner_poll(
            poll["id"], st["days"], st["voted"], tg.mention_list(st["waiting"]),
            t.parse_iso(poll["deadline_at"]), st["missing"])
    await _show_poll(bot, poll["id"], text, markup, bump)


def schedule_render(bot: Bot, poll_id: int) -> None:
    """Coalesce rapid toggles into one edit."""
    tg.debouncer.trigger(f"dinner:{poll_id}", lambda: render(bot, poll_id))


def phase_for(day: str) -> str:
    """Which headline the locked-dinner card should be wearing right now."""
    gap = (t.parse_date(day) - t.today_local()).days
    if gap <= 0:
        return "day"
    if gap == 1:
        return "t1"
    return "locked"


async def render_event(bot: Bot, event_id: int, phase: str | None = None,
                       bump: bool = False, locked_by: str | None = None) -> None:
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (event_id,))
    if event is None:
        return
    day = event["dinner_date"]
    text, markup = boards.dinner_event_card(
        event_id, day, tg.mention_list(attendees(event_id)), absentees(event_id),
        phase or phase_for(day), locked_by)
    await _show_event(bot, event_id, text, markup, bump)


# --- opening / closing -----------------------------------------------------

async def open_new(bot: Bot, opened_by: int | None, start: date | None = None,
                   adopt_message_id: int | None = None,
                   days: int | None = None) -> int:
    """Start a vote. `adopt_message_id` reuses the box the last one left behind."""
    days = days or db.get_int("dinner_window_days")
    # Candidate days start tomorrow - dinner needs runway (spec 5.2).
    first = start or _earliest()
    deadline = t.now_utc() + timedelta(hours=db.get_int("poll_deadline_hours"))
    cur = db.x(
        "INSERT INTO dinner_polls (status, opened_by, opened_at, deadline_at, "
        "start_date, days, message_id) VALUES ('open', ?, ?, ?, ?, ?, ?)",
        (opened_by, t.iso(t.now_utc()), t.iso(deadline), first.isoformat(), days,
         adopt_message_id))
    poll_id = int(cur.lastrowid)

    db.add_job(deadline, "dinner_deadline", poll_id, respect_quiet=False)
    db.add_job(t.now_utc() + timedelta(hours=db.get_int("dinner_nag_hours")),
               "dinner_nag", poll_id)
    await render(bot, poll_id)
    return poll_id


def close_poll(poll_id: int, status: str) -> None:
    db.x("UPDATE dinner_polls SET status = ? WHERE id = ?", (status, poll_id))
    db.cancel_jobs("dinner_nag", poll_id)
    db.cancel_jobs("dinner_deadline", poll_id)


def _release_box(poll_id: int) -> int | None:
    """Hand the poll's message over to whatever comes next, and forget it."""
    poll = db.q1("SELECT message_id FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None:
        return None
    db.x("UPDATE dinner_polls SET message_id = NULL WHERE id = ?", (poll_id,))
    return poll["message_id"]


async def clear_board(bot: Bot, poll_id: int) -> None:
    """Empty the dinner box after a vote is killed from outside (admin /cancel).

    Closing the poll row is not enough on its own: the board would still be
    sitting in the group with live buttons on a vote that no longer exists.
    """
    text, markup = boards.dinner_vote_cancelled()
    await _show_poll(bot, poll_id, text, markup, bump=False)


async def lock(bot: Bot, poll_id: int, day: str, by_user: int) -> bool:
    """Lock a date in. Guarded so two simultaneous taps cannot double-book."""
    cur = db.x("UPDATE dinner_polls SET status = 'locked', locked_date = ? "
               "WHERE id = ? AND status = 'open'", (day, poll_id))
    if cur.rowcount == 0:
        return False
    db.cancel_jobs("dinner_nag", poll_id)
    db.cancel_jobs("dinner_deadline", poll_id)

    box = _release_box(poll_id)
    cur = db.x("INSERT INTO dinner_events (poll_id, dinner_date, status, created_at, "
               "message_id) VALUES (?, ?, 'upcoming', ?, ?)",
               (poll_id, day, t.iso(t.now_utc()), box))
    event_id = int(cur.lastrowid)
    _snapshot_attendees(event_id, poll_id, day)
    schedule_event_jobs(event_id, day)

    # Worth a ping: the vote box becomes the dinner card, at the bottom.
    await render_event(bot, event_id, "locked", bump=True,
                       locked_by=db.name_of(by_user))
    return True


def schedule_event_jobs(event_id: int, day: str) -> None:
    """T-3, T-1, day-of and the completion sweep. Past ones are simply skipped."""
    dd = t.parse_date(day)
    hour = db.get_int("reminder_hour")
    now = t.now_utc()
    for offset, tag in ((3, "t3"), (1, "t1"), (0, "day")):
        when = t.local_at(dd - timedelta(days=offset), hour)
        if when > now:
            db.add_job(when, "dinner_remind", event_id, tag)
    db.add_job(t.local_at(dd, db.get_int("dinner_done_hour")), "dinner_done",
               event_id, respect_quiet=False)


def cancel_event(event_id: int) -> str | None:
    row = db.q1("SELECT * FROM dinner_events WHERE id = ?", (event_id,))
    if row is None or row["status"] != "upcoming":
        return None
    db.x("UPDATE dinner_events SET status = 'cancelled' WHERE id = ?", (event_id,))
    db.cancel_jobs("dinner_remind", event_id)
    db.cancel_jobs("dinner_done", event_id)
    return row["dinner_date"]


# --- choosing the dates ----------------------------------------------------
# The picker is the dinner box before a vote exists: the same message walks
# through "which days?" -> first day -> last day and then becomes the vote
# board. Like every other flow it is stateless - the chosen start rides in
# the callback token - so the box is simply whichever message was tapped.

PICK_PAGE = 14                          # dates per picker page
_PICKER = "dinner_picker_message_id"    # the picker /dinner last posted


def _earliest() -> date:
    return t.today_local() + timedelta(days=1)


def _pages(total: int) -> int:
    return max(1, -(-total // PICK_PAGE))


async def _show_picker(bot: Bot, message_id: int | None, text, markup,
                       bump: bool = False) -> None:
    chat_id = db.group_chat_id()
    if chat_id is None:
        return
    tracked = db.get_setting(_PICKER)
    if bump:
        new_id = await tg.repost(bot, chat_id, message_id, text, markup,
                                 boards.dinner_board_moved())
    else:
        new_id = await tg.edit_or_repost(bot, chat_id, message_id, text, markup)
    if bump or (tracked and tracked == str(message_id)):
        db.set_setting(_PICKER, new_id or "")
    if new_id and message_id is not None and new_id != message_id:
        # The picker sat on a dinner card that could not be edited: the card
        # follows it, so the dinner still knows which message is its box.
        db.x("UPDATE dinner_events SET message_id = ? WHERE message_id = ?",
             (new_id, message_id))


def _forget_picker(message_id: int) -> None:
    if db.get_setting(_PICKER) == str(message_id):
        db.set_setting(_PICKER, "")


async def _picker_home(bot: Bot, message_id: int | None, bump: bool = False) -> None:
    text, markup = boards.dinner_when(_earliest(), db.get_int("dinner_window_days"))
    await _show_picker(bot, message_id, text, markup, bump)


async def _picker_start(bot: Bot, box: int, page: int) -> None:
    horizon = db.get_int("dinner_horizon_days")
    page = min(max(page, 0), _pages(horizon) - 1)
    dates = t.date_range(horizon)[page * PICK_PAGE:(page + 1) * PICK_PAGE]
    text, markup = boards.dinner_pick_start(dates, page, _pages(horizon))
    await _show_picker(bot, box, text, markup)


async def _picker_end(bot: Bot, box: int, start: str, page: int) -> None:
    longest = db.get_int("dinner_max_range_days")
    page = min(max(page, 0), _pages(longest) - 1)
    dates = t.date_range(longest, t.parse_date(start))
    dates = dates[page * PICK_PAGE:(page + 1) * PICK_PAGE]
    text, markup = boards.dinner_pick_end(start, dates, page, _pages(longest))
    await _show_picker(bot, box, text, markup)


async def _picker_close(bot: Bot, box: int) -> None:
    """Never mind. A picker opened over a locked dinner hands the card back."""
    event = upcoming_event()
    if event is not None and event["message_id"] == box:
        await render_event(bot, event["id"])
        return
    text, markup = boards.dinner_vote_cancelled()
    await _show_picker(bot, box, text, markup)
    _forget_picker(box)


async def _start_range(update, bot: Bot, box: int, first_s: str, last_s: str,
                       user_id: int) -> None:
    """The last tap of the picker: open the vote over first..last inclusive."""
    try:
        first, last = t.parse_date(first_s), t.parse_date(last_s)
    except ValueError:
        await tg.toast(update, boards.dinner_dates_stale(), alert=True)
        return
    days = (last - first).days + 1
    # A picker left overnight still offers yesterday's "tomorrow".
    if first < _earliest() or not 1 <= days <= db.get_int("dinner_max_range_days"):
        await tg.toast(update, boards.dinner_dates_stale(), alert=True)
        await _picker_home(bot, box)
        return
    if open_poll() is not None:
        await tg.toast(update, boards.dinner_already_running(), alert=True)
        return
    event = upcoming_event()
    if event is not None:
        # Only reachable by design from "Pick another day" on the dinner card:
        # the dinner is dropped now, at the moment its replacement vote opens,
        # so backing out of the picker leaves the dinner standing.
        if event["message_id"] != box:
            await tg.toast(update, boards.dinner_already_running(), alert=True)
            return
        if not db.is_admin(user_id):
            await tg.toast(update, boards.dinner_not_admin(_admin_name()), alert=True)
            return
        cancel_event(event["id"])
    await tg.toast(update, "")
    # Whatever owned this message lets go first, so only one row owns it.
    db.x("UPDATE dinner_events SET message_id = NULL WHERE message_id = ?", (box,))
    _forget_picker(box)
    await open_new(bot, user_id, first, adopt_message_id=box, days=days)


# --- entry point (/dinner) -------------------------------------------------

async def entry(bot: Bot, user_id: int) -> None:
    """/dinner never adds to the chat.

    If a dinner is already in flight, the existing box is moved down to the
    bottom where the asker is looking - same single message, no duplicate
    board, nothing to scroll back for. Only a genuinely fresh start creates
    anything, and what it creates is the date picker - which is itself moved
    rather than repeated if /dinner is sent again before anyone picks.
    """
    event = upcoming_event()
    if event is not None:
        await render_event(bot, event["id"], bump=True)
        return

    poll = open_poll()
    if poll is not None:
        await render(bot, poll["id"], bump=True)
        return

    tracked = db.get_setting(_PICKER)
    await _picker_home(bot, int(tracked) if tracked else None, bump=True)


# --- callbacks -------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = tg.parse_cb(update.callback_query.data)
    action = parts[1]
    user = update.effective_user
    bot = context.bot

    if db.member_by_user(user.id) is None:
        await tg.toast(update, boards.not_registered(), alert=True)
        return

    if action == "menu":
        await tg.toast(update, "")
        await entry(bot, user.id)

    elif action == "open":
        await tg.toast(update, "")
        if open_poll() is None and upcoming_event() is None:
            # Take over whichever box the tap came from, so a finished dinner
            # turns into the picker, and then the new vote, rather than
            # sitting beside it.
            await _picker_home(bot, update.callback_query.message.message_id)

    elif action == "pm":
        await tg.toast(update, "")
        await _picker_home(bot, update.callback_query.message.message_id)

    elif action == "ps":
        await tg.toast(update, "")
        await _picker_start(bot, update.callback_query.message.message_id,
                            int(parts[2]))

    elif action == "pe":
        await tg.toast(update, "")
        await _picker_end(bot, update.callback_query.message.message_id,
                          parts[2], int(parts[3]))

    elif action == "pc":
        await tg.toast(update, "")
        await _picker_close(bot, update.callback_query.message.message_id)

    elif action == "go":
        await _start_range(update, bot, update.callback_query.message.message_id,
                           parts[2], parts[3], user.id)

    elif action == "board":
        await tg.toast(update, "")
        await render(bot, int(parts[2]))

    elif action == "v":
        await _toggle(update, bot, int(parts[2]), parts[3], user.id)

    elif action == "none":
        await _vote_none(update, bot, int(parts[2]), user.id)

    elif action == "lock":
        poll_id, day = int(parts[2]), parts[3]
        poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
        if poll is None or poll["status"] != "open":
            await tg.toast(update, "That vote is already closed.", alert=True)
            return
        await tg.toast(update, "")
        # The confirm step happens inside the vote box, not beside it.
        text, markup = boards.dinner_lock_confirm(poll_id, day)
        await _show_poll(bot, poll_id, text, markup, bump=False)

    elif action == "lockc":
        poll_id, day = int(parts[2]), parts[3]
        if await lock(bot, poll_id, day, user.id):
            await tg.toast(update, "Locked in 🔒")
        else:
            await tg.toast(update, "Someone just beat you to it 😄", alert=True)

    elif action == "fin":
        # Admin-only finalize. Unlike "lock", this can pick a non-unanimous day
        # (>=1 vote), so it is gated on admin here.
        poll_id, day = int(parts[2]), parts[3]
        if not db.is_admin(user.id):
            await tg.toast(update, boards.dinner_not_admin(_admin_name()), alert=True)
            return
        poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
        if poll is None or poll["status"] != "open":
            await tg.toast(update, "That vote is already closed.", alert=True)
            return
        if await lock(bot, poll_id, day, user.id):
            await tg.toast(update, "Locked in 🔒")
        else:
            await tg.toast(update, "Someone just beat you to it 😄", alert=True)

    elif action in ("kill", "drop"):
        poll_id = int(parts[2])
        close_poll(poll_id, "cancelled")
        await tg.toast(update, "")
        text, markup = boards.dinner_vote_cancelled()
        await _show_poll(bot, poll_id, text, markup, bump=False)

    elif action == "ext":
        poll_id = int(parts[2])
        poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
        close_poll(poll_id, "expired")
        await tg.toast(update, "")
        # Votes reset; the window rolls forward to the days after the old ones,
        # and the new vote takes over the same box.
        nxt = t.parse_date(poll["start_date"]) + timedelta(days=poll["days"])
        start = max(nxt, t.today_local() + timedelta(days=1))
        await open_new(bot, user.id, start, adopt_message_id=_release_box(poll_id))

    elif action == "revote":
        # Admin changed their mind: the dinner card turns into the date picker.
        # The dinner itself is only dropped once new dates are chosen (see
        # _start_range), so "Never mind" puts the card straight back.
        event_id = int(parts[2])
        if not db.is_admin(user.id):
            await tg.toast(update, boards.dinner_not_admin(_admin_name()), alert=True)
            return
        event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (event_id,))
        if event is None or event["status"] != "upcoming":
            await tg.toast(update, "That dinner isn't on any more.", alert=True)
            return
        await tg.toast(update, "")
        await _picker_home(bot, update.callback_query.message.message_id)

    elif action == "ecancel":
        event_id = int(parts[2])
        if not db.is_admin(user.id):
            await tg.toast(update, boards.dinner_not_admin(_admin_name()), alert=True)
            return
        day = cancel_event(event_id)
        await tg.toast(update, "")
        if day:
            text, markup = boards.dinner_event_cancelled(day)
            await _show_event(bot, event_id, text, markup, bump=False)


async def _toggle(update, bot, poll_id: int, day: str, user_id: int) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None or poll["status"] != "open":
        await tg.toast(update, "That vote is closed.", alert=True)
        return
    existing = db.q1("SELECT 1 FROM dinner_votes WHERE poll_id = ? AND user_id = ? "
                     "AND vote_date = ?", (poll_id, user_id, day))
    if existing:
        db.x("DELETE FROM dinner_votes WHERE poll_id = ? AND user_id = ? AND vote_date = ?",
             (poll_id, user_id, day))
        await tg.toast(update, f"Removed {t.fmt_date(day)}")
    else:
        # Picking a day means you are no longer claiming that no day works.
        db.x("DELETE FROM dinner_votes WHERE poll_id = ? AND user_id = ? AND vote_date = ?",
             (poll_id, user_id, NONE))
        db.x("INSERT OR IGNORE INTO dinner_votes (poll_id, user_id, vote_date) "
             "VALUES (?, ?, ?)", (poll_id, user_id, day))
        await tg.toast(update, f"✅ {t.fmt_date(day)}")
    schedule_render(bot, poll_id)


async def _vote_none(update, bot, poll_id: int, user_id: int) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None or poll["status"] != "open":
        await tg.toast(update, "That vote is closed.", alert=True)
        return
    db.x("DELETE FROM dinner_votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    db.x("INSERT OR IGNORE INTO dinner_votes (poll_id, user_id, vote_date) "
         "VALUES (?, ?, ?)", (poll_id, user_id, NONE))
    await tg.toast(update, "Noted — no days work for you.")
    schedule_render(bot, poll_id)


# --- scheduled jobs --------------------------------------------------------

@scheduler.on("dinner_nag")
async def _job_nag(bot: Bot, job) -> None:
    """Chase non-voters by moving the board, not by adding to the chat.

    The board already lists who it is waiting on, as mentions, so re-posting
    it pings exactly those people and leaves the group one message richer
    than before: zero.
    """
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (job["ref_id"],))
    if poll is None or poll["status"] != "open":
        return
    # Voting is over: there is nobody left to chase, and the box is showing the
    # pick-a-day prompt. A batch of overdue jobs can hand us a nag that the
    # deadline already cancelled, so this is checked rather than assumed.
    if past_deadline(poll):
        return
    if poll_state(poll)["waiting"]:
        await render(bot, poll["id"], bump=True)
    # Re-arm until the deadline closes the poll.
    db.add_job(t.now_utc() + timedelta(hours=db.get_int("dinner_nag_hours")),
               "dinner_nag", poll["id"])


@scheduler.on("dinner_deadline")
async def _job_deadline(bot: Bot, job) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (job["ref_id"],))
    if poll is None or poll["status"] != "open":
        return
    db.cancel_jobs("dinner_nag", poll["id"])
    # Stamp the close time, so "voting is over" is a fact about the poll rather
    # than a guess from the clock. render() then switches the same box over to
    # the pick-a-day prompt - and a bot that was offline past its own deadline
    # still reads as closed when it wakes up.
    db.x("UPDATE dinner_polls SET deadline_at = ? WHERE id = ?",
         (t.iso(t.now_utc()), poll["id"]))
    await render(bot, poll["id"], bump=True)


@scheduler.on("dinner_remind")
async def _job_remind(bot: Bot, job) -> None:
    """T-3 / T-1 / day-of: the same dinner card, re-posted with a new headline.

    It has to be a re-post rather than an edit, because an edit pings nobody -
    and the mentions it carries are only the people who said they were coming.
    """
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (job["ref_id"],))
    if event is None or event["status"] != "upcoming":
        return
    await render_event(bot, event["id"], job["payload"] or "t1", bump=True)


@scheduler.on("dinner_done")
async def _job_done(bot: Bot, job) -> None:
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (job["ref_id"],))
    if event is None or event["status"] != "upcoming":
        return
    db.x("UPDATE dinner_events SET status = 'done' WHERE id = ?", (event["id"],))
    # Quietly retire the card: an edit, so nobody's phone buzzes at 10pm.
    text, markup = boards.dinner_event_done(event["dinner_date"])
    await _show_event(bot, event["id"], text, markup, bump=False)
