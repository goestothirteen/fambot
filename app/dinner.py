"""Feature A - the dinner scheduler.

The engine: open a 7-day poll, everyone multi-selects, a day becomes viable
only when all five have ticked it, and then a human locks it in. The bot
never picks the day itself (spec 2.2).
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


# --- rendering -------------------------------------------------------------

async def render(bot: Bot, poll_id: int) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    if poll is None or poll["status"] != "open":
        return
    st = poll_state(poll)
    text, markup = boards.dinner_poll(
        poll["id"], st["days"], st["voted"], [m["name"] for m in st["waiting"]],
        t.parse_iso(poll["deadline_at"]), st["missing"])
    chat_id = db.group_chat_id()
    if chat_id is None:
        return
    new_id = await tg.edit_or_repost(bot, chat_id, poll["message_id"], text, markup)
    if new_id and new_id != poll["message_id"]:
        db.x("UPDATE dinner_polls SET message_id = ? WHERE id = ?", (new_id, poll["id"]))


def schedule_render(bot: Bot, poll_id: int) -> None:
    """Coalesce rapid toggles into one edit."""
    tg.debouncer.trigger(f"dinner:{poll_id}", lambda: render(bot, poll_id))


# --- opening / closing -----------------------------------------------------

async def open_new(bot: Bot, opened_by: int | None, start: date | None = None) -> int:
    days = db.get_int("dinner_window_days")
    # Candidate days start tomorrow - dinner needs runway (spec 5.2).
    first = start or (t.today_local() + timedelta(days=1))
    deadline = t.now_utc() + timedelta(hours=db.get_int("poll_deadline_hours"))
    cur = db.x(
        "INSERT INTO dinner_polls (status, opened_by, opened_at, deadline_at, "
        "start_date, days) VALUES ('open', ?, ?, ?, ?, ?)",
        (opened_by, t.iso(t.now_utc()), t.iso(deadline), first.isoformat(), days))
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


async def lock(bot: Bot, poll_id: int, day: str, by_user: int) -> bool:
    """Lock a date in. Guarded so two simultaneous taps cannot double-book."""
    cur = db.x("UPDATE dinner_polls SET status = 'locked', locked_date = ? "
               "WHERE id = ? AND status = 'open'", (day, poll_id))
    if cur.rowcount == 0:
        return False
    db.cancel_jobs("dinner_nag", poll_id)
    db.cancel_jobs("dinner_deadline", poll_id)

    cur = db.x("INSERT INTO dinner_events (poll_id, dinner_date, status, created_at) "
               "VALUES (?, ?, 'upcoming', ?)", (poll_id, day, t.iso(t.now_utc())))
    event_id = int(cur.lastrowid)
    schedule_event_jobs(event_id, day)

    text, markup = boards.dinner_locked(event_id, day, db.name_of(by_user))
    chat_id = db.group_chat_id()
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
    if chat_id is not None:
        msg_id = await tg.edit_or_repost(bot, chat_id, poll["message_id"], text, markup)
        db.x("UPDATE dinner_events SET message_id = ? WHERE id = ?", (msg_id, event_id))
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


# --- entry point (🍜 button / /dinner) -------------------------------------

async def entry(bot: Bot, user_id: int) -> None:
    event = upcoming_event()
    if event is not None:
        dropouts = [db.name_of(r["user_id"]) for r in
                    db.q("SELECT user_id FROM dinner_dropouts WHERE event_id = ?",
                         (event["id"],))]
        text, markup = boards.dinner_view_locked(
            event["id"], event["dinner_date"], dropouts, db.is_admin(user_id))
        await tg.send_group(bot, text, markup)
        return

    poll = open_poll()
    if poll is not None:
        # Bump the live board rather than starting a duplicate (spec 4.1).
        db.x("UPDATE dinner_polls SET message_id = NULL WHERE id = ?", (poll["id"],))
        await render(bot, poll["id"])
        return

    text, markup = boards.dinner_start_prompt(db.get_int("dinner_window_days"))
    await tg.send_group(bot, text, markup)


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
            await open_new(bot, user.id)

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
        text, markup = boards.dinner_lock_confirm(poll_id, day)
        await tg.send_group(bot, text, markup)

    elif action == "lockc":
        poll_id, day = int(parts[2]), parts[3]
        if await lock(bot, poll_id, day, user.id):
            await tg.toast(update, "Locked in 🔒")
        else:
            await tg.toast(update, "Someone just beat you to it 😄", alert=True)

    elif action == "kill":
        poll_id = int(parts[2])
        close_poll(poll_id, "cancelled")
        await tg.toast(update, "")
        await tg.send_group(bot, boards.dinner_poll_cancelled())

    elif action == "ext":
        poll_id = int(parts[2])
        poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (poll_id,))
        close_poll(poll_id, "expired")
        await tg.toast(update, "")
        # Votes reset; the window rolls forward to the days after the old ones.
        nxt = t.parse_date(poll["start_date"]) + timedelta(days=poll["days"])
        start = max(nxt, t.today_local() + timedelta(days=1))
        await open_new(bot, user.id, start)

    elif action == "drop":
        close_poll(int(parts[2]), "cancelled")
        await tg.toast(update, "")
        await tg.send_group(bot, boards.dinner_dropped())

    elif action in ("in", "out"):
        await _rsvp(update, bot, int(parts[2]), user.id, action == "in")

    elif action == "revote":
        event_id = int(parts[2])
        cancel_event(event_id)
        await tg.toast(update, "")
        await open_new(bot, user.id)

    elif action == "ecancel":
        event_id = int(parts[2])
        day = cancel_event(event_id)
        await tg.toast(update, "")
        if day:
            await tg.send_group(bot, boards.dinner_cancelled(day))


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


async def _rsvp(update, bot, event_id: int, user_id: int, coming: bool) -> None:
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (event_id,))
    if event is None or event["status"] != "upcoming":
        await tg.toast(update, "That dinner isn't on any more.", alert=True)
        return
    who = db.name_of(user_id)
    if coming:
        db.x("DELETE FROM dinner_dropouts WHERE event_id = ? AND user_id = ?",
             (event_id, user_id))
        await tg.toast(update, "👍 Thanks!")
        return
    db.x("INSERT OR IGNORE INTO dinner_dropouts (event_id, user_id) VALUES (?, ?)",
         (event_id, user_id))
    await tg.toast(update, "OK, I've let everyone know.")
    text, markup = boards.dinner_dropout(event_id, who, event["dinner_date"])
    await tg.send_group(bot, text, markup)


# --- scheduled jobs --------------------------------------------------------

@scheduler.on("dinner_nag")
async def _job_nag(bot: Bot, job) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (job["ref_id"],))
    if poll is None or poll["status"] != "open":
        return
    waiting = poll_state(poll)["waiting"]
    if waiting:
        await tg.send_group(bot, boards.dinner_nag(tg.mention_list(waiting)))
    # Re-arm until the deadline closes the poll.
    db.add_job(t.now_utc() + timedelta(hours=db.get_int("dinner_nag_hours")),
               "dinner_nag", poll["id"])


@scheduler.on("dinner_deadline")
async def _job_deadline(bot: Bot, job) -> None:
    poll = db.q1("SELECT * FROM dinner_polls WHERE id = ?", (job["ref_id"],))
    if poll is None or poll["status"] != "open":
        return
    db.cancel_jobs("dinner_nag", poll["id"])
    st = poll_state(poll)
    viable = [d for d in st["days"] if d["viable"]]
    if viable:
        # Leave the poll open so the lock buttons still work; nagging stops.
        text, markup = boards.dinner_deadline_viable(poll["id"], viable)
        await tg.send_group(bot, text, markup)
        return
    close_poll(poll["id"], "expired")
    text, markup = boards.dinner_deadline_none(poll["id"], poll["days"])
    await tg.send_group(bot, text, markup)


@scheduler.on("dinner_remind")
async def _job_remind(bot: Bot, job) -> None:
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (job["ref_id"],))
    if event is None or event["status"] != "upcoming":
        return
    text, markup = boards.dinner_reminder(event["id"], event["dinner_date"],
                                          job["payload"] or "t1")
    await tg.send_group(bot, text, markup)


@scheduler.on("dinner_done")
async def _job_done(bot: Bot, job) -> None:
    event = db.q1("SELECT * FROM dinner_events WHERE id = ?", (job["ref_id"],))
    if event is None or event["status"] != "upcoming":
        return
    db.x("UPDATE dinner_events SET status = 'done' WHERE id = ?", (event["id"],))
    await tg.send_group(bot, boards.dinner_done(event["dinner_date"]))
