"""Feature C - the rolling Sunday roster.

Rolling, not calendar-month (spec 7): a round covers the next five Sundays
nobody holds yet, so the roster can start today instead of waiting for a
month boundary. Rounds are only ever opened by a person, from /roster - the
bot does not start one on its own.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import timedelta

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app import boards, db, scheduler, tg
from app import timeutil as t

log = logging.getLogger(__name__)

FAR_PAST = "0000-00-00"   # sorts before any real date, for someone with no history


# --- the fairness rule (pure, so it can be tested) -------------------------

def assign_slots(dates: list[str], available: dict[str, list[int]],
                 counts: dict[int, int], last_duty: dict[int, str],
                 jitter: dict[int, float] | None = None) -> dict[str, int | None]:
    """Share the Sundays out among whoever said they were free.

    For each Sunday, among the kids who marked themselves available, take
    the one with the fewest duties in the trailing window; break ties by
    who last did one longest ago, then at random (spec 7.2.3).

    Assignments made inside this run count immediately, so one kid does not
    collect every Sunday just because they answered first. A Sunday nobody
    marked comes back as None - the bot flags it rather than conscripting
    somebody.
    """
    counts = dict(counts)
    last_duty = dict(last_duty)
    if jitter is None:
        jitter = {}
    out: dict[str, int | None] = {}

    for day in sorted(dates):
        candidates = available.get(day, [])
        if not candidates:
            out[day] = None
            continue
        for uid in candidates:
            jitter.setdefault(uid, random.random())
        chosen = min(candidates, key=lambda u: (counts.get(u, 0),
                                                last_duty.get(u, FAR_PAST),
                                                jitter[u]))
        out[day] = chosen
        counts[chosen] = counts.get(chosen, 0) + 1
        last_duty[chosen] = max(last_duty.get(chosen, FAR_PAST), day)
    return out


# --- queries ---------------------------------------------------------------

def open_round():
    return db.q1("SELECT * FROM roster_rounds WHERE status = 'collecting' "
                 "ORDER BY id DESC LIMIT 1")


def round_dates(rnd) -> list[str]:
    return json.loads(rnd["dates"])


def future_slots(limit: int | None = None) -> list:
    today = t.today_local().isoformat()
    sql = "SELECT * FROM roster_slots WHERE duty_date >= ? ORDER BY duty_date"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.q(sql, (today,))


def assigned_future_count() -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM roster_slots WHERE duty_date >= ? AND assignee IS NOT NULL",
        (t.today_local().isoformat(),), default=0))


def has_roster() -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM roster_slots", default=0))


def covered_sundays() -> list[dict]:
    """Every future Sunday somebody already holds, soonest first."""
    return [{"date": s["duty_date"], "name": db.name_of(s["assignee"])}
            for s in future_slots() if s["assignee"] is not None]


def next_open_sundays(count: int) -> list[str]:
    """The next `count` Sundays nobody holds yet.

    Covered Sundays are stepped over rather than asked about again, so a
    round started by hand always breaks new ground: with the roster filled
    to the 18th, it asks about the 25th onwards.
    """
    taken = {c["date"] for c in covered_sundays()}
    sundays = [d.isoformat() for d in t.next_sundays(count + len(taken))]
    return [d for d in sundays if d not in taken][:count]


def ensure_slots(dates: list[str]) -> None:
    for day in dates:
        db.x("INSERT OR IGNORE INTO roster_slots (duty_date, status) "
             "VALUES (?, 'collecting')", (day,))


# --- rendering -------------------------------------------------------------

def round_state(rnd) -> dict:
    dates = round_dates(rnd)
    avail: dict[str, list[int]] = {d: [] for d in dates}
    for r in db.q("SELECT duty_date, user_id FROM roster_availability "
                  "WHERE round_id = ? AND available = 1", (rnd["id"],)):
        avail.setdefault(r["duty_date"], []).append(r["user_id"])
    responded = {r["user_id"] for r in
                 db.q("SELECT user_id FROM roster_responses WHERE round_id = ?",
                      (rnd["id"],))}
    kids = db.kids()
    return {
        "days": [{"date": d, "count": len(avail.get(d, [])),
                  "names": [db.name_of(u) for u in avail.get(d, [])]} for d in dates],
        "available": avail,
        "responded": [m["name"] for m in kids if m["user_id"] in responded],
        "waiting": [m for m in kids if m["user_id"] not in responded],
    }


_BOX = "roster_box_message_id"


def _slot_data() -> list[dict]:
    return [{"date": s["duty_date"], "assignee": s["assignee"],
             "name": db.name_of(s["assignee"]) if s["assignee"] else None}
            for s in future_slots()]


def _help_data() -> list[dict]:
    today = t.today_local()
    until = (today + timedelta(days=14)).isoformat()
    return [{"kind": h["kind"], "req_date": h["req_date"],
             "time_window": h["time_window"],
             "claimant": db.name_of(h["claimed_by"]) if h["claimed_by"] else None}
            for h in db.q("SELECT * FROM help_requests WHERE status IN ('open','claimed') "
                          "AND req_date >= ? AND req_date <= ? ORDER BY req_date",
                          (today.isoformat(), until))]


async def render_box(bot: Bot, bump: bool = False, headline: str | None = None) -> None:
    """Draw the one roster box: the vote while a round runs, the roster otherwise.

    bump=False edits in place - instant, and nobody's phone lights up.
    bump=True takes the box down and re-posts it at the bottom, which is the
    only way a chase-up, a reminder or a swap request actually reaches anyone.
    `headline` says why it moved.
    """
    chat_id = db.group_chat_id()
    if chat_id is None:
        return
    rnd = open_round()
    if rnd is not None:
        st = round_state(rnd)
        # Mentions, not names: a re-posted board is how the silent kids get chased.
        text, markup = boards.roster_round(
            rnd["id"], st["days"], st["responded"], tg.mention_list(st["waiting"]),
            t.parse_iso(rnd["deadline_at"]), covered_sundays(), headline)
    else:
        text, markup = boards.roster_card(_slot_data(), _help_data(), has_roster(),
                                          headline)
    old = db.get_setting(_BOX)
    old_id = int(old) if old else None
    if bump:
        new_id = await tg.repost(bot, chat_id, old_id, text, markup,
                                 boards.roster_board_moved())
    else:
        new_id = await tg.edit_or_repost(bot, chat_id, old_id, text, markup)
    if new_id and new_id != old_id:
        db.set_setting(_BOX, new_id)


def schedule_render(bot: Bot, round_id: int) -> None:
    tg.debouncer.trigger(f"roster:{round_id}", lambda: render_box(bot))


# --- availability rounds ---------------------------------------------------

async def start_round(bot: Bot, dates: list[str] | None = None) -> int | None:
    if not db.kids() or open_round() is not None:
        return None

    if dates is None:
        dates = next_open_sundays(db.get_int("roster_horizon"))
    if not dates:
        return None

    ensure_slots(dates)
    deadline = t.now_utc() + timedelta(hours=db.get_int("roster_deadline_hours"))
    cur = db.x("INSERT INTO roster_rounds (status, opened_at, deadline_at, dates) "
               "VALUES ('collecting', ?, ?, ?)",
               (t.iso(t.now_utc()), t.iso(deadline), json.dumps(dates)))
    round_id = int(cur.lastrowid)
    db.add_job(deadline, "roster_deadline", round_id, respect_quiet=False)
    db.add_job(t.now_utc() + timedelta(hours=db.get_int("roster_nag_hours")),
               "roster_nag", round_id)
    # Worth a ping: the box becomes the vote, at the bottom.
    await render_box(bot, bump=True)
    return round_id


async def close_round(bot: Bot, round_id: int) -> None:
    """Deadline reached (or everyone answered) - share the Sundays out."""
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id = ?", (round_id,))
    if rnd is None or rnd["status"] != "collecting":
        return
    db.x("UPDATE roster_rounds SET status = 'closed' WHERE id = ?", (round_id,))
    db.cancel_jobs("roster_nag", round_id)
    db.cancel_jobs("roster_deadline", round_id)
    db.cancel_jobs("roster_close", round_id)

    st = round_state(rnd)
    dates = round_dates(rnd)
    # Never reassign a Sunday somebody already holds.
    taken = {r["duty_date"] for r in db.q(
        "SELECT duty_date FROM roster_slots WHERE assignee IS NOT NULL")}
    todo = [d for d in dates if d not in taken]

    result = assign_slots(todo, st["available"], db.duty_counts(), db.last_duty_dates())
    for day, uid in result.items():
        if uid is None:
            db.x("UPDATE roster_slots SET status = 'unfilled' WHERE duty_date = ?", (day,))
        else:
            set_assignee(day, uid)

    # The vote turns into the roster, and moves so everyone sees who got what.
    # Any Sunday nobody could do is flagged on it with a claim button.
    await render_box(bot, bump=True, headline=boards.roster_shared_out())


def set_assignee(day: str, user_id: int) -> None:
    db.x("UPDATE roster_slots SET assignee = ?, status = 'assigned' WHERE duty_date = ?",
         (user_id, day))
    db.log_duty(user_id, day, "roster", None)
    schedule_slot_reminders(day)


def clear_assignee(day: str) -> int | None:
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date = ?", (day,))
    if slot is None or slot["assignee"] is None:
        return None
    old = slot["assignee"]
    db.x("UPDATE roster_slots SET assignee = NULL, status = 'open_swap' "
         "WHERE duty_date = ?", (day,))
    db.x("DELETE FROM duty_log WHERE user_id = ? AND duty_date = ? AND kind = 'roster'",
         (old, day))
    db.cancel_jobs("roster_remind", slot["id"])
    return old


def schedule_slot_reminders(day: str) -> None:
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date = ?", (day,))
    if slot is None:
        return
    db.cancel_jobs("roster_remind", slot["id"])
    dd = t.parse_date(day)
    now = t.now_utc()
    saturday = t.local_at(dd - timedelta(days=1), 20)
    sunday = t.local_at(dd, 12)
    if saturday > now:
        db.add_job(saturday, "roster_remind", slot["id"], "sat")
    if sunday > now:
        db.add_job(sunday, "roster_remind", slot["id"], "sun")


# --- entry point (/roster) -------------------------------------------------

async def entry(bot: Bot) -> None:
    """/roster never adds to the chat: it moves the one box down to the asker."""
    await render_box(bot, bump=True)


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
        await entry(bot)

    elif action == "view":
        # Only still on roster messages posted before the one-box change.
        await tg.toast(update, "")
        await render_box(bot)

    elif action == "setup":
        if not db.kids():
            await tg.toast(update, boards.roster_no_kids(), alert=True)
        elif open_round() is not None:
            await tg.toast(update, boards.roster_round_open(), alert=True)
        else:
            await tg.toast(update, "")
            await start_round(bot)

    elif action == "av":
        await _toggle(update, bot, int(parts[2]), parts[3], user.id)

    elif action == "none":
        await _none(update, bot, int(parts[2]), user.id)

    elif action == "take":
        await _take(update, bot, parts[2], user.id)

    elif action == "swap":
        await _swap(update, bot, parts[2], user.id)


async def _toggle(update, bot, round_id: int, day: str, user_id: int) -> None:
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id = ?", (round_id,))
    if rnd is None or rnd["status"] != "collecting":
        await tg.toast(update, "That round is closed.", alert=True)
        return
    if not db.is_kid(user_id):
        await tg.toast(update, boards.kids_only(), alert=True)
        return

    row = db.q1("SELECT available FROM roster_availability WHERE duty_date = ? "
                "AND user_id = ?", (day, user_id))
    now_available = 0 if (row and row["available"]) else 1
    db.x("INSERT INTO roster_availability (duty_date, user_id, available, round_id) "
         "VALUES (?, ?, ?, ?) ON CONFLICT(duty_date, user_id) DO UPDATE SET "
         "available = excluded.available, round_id = excluded.round_id",
         (day, user_id, now_available, round_id))
    db.x("INSERT OR IGNORE INTO roster_responses (round_id, user_id) VALUES (?, ?)",
         (round_id, user_id))
    await tg.toast(update, ("✅ " if now_available else "Removed ") + t.fmt_date(day))
    schedule_render(bot, round_id)
    await _maybe_close(bot, round_id)


async def _none(update, bot, round_id: int, user_id: int) -> None:
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id = ?", (round_id,))
    if rnd is None or rnd["status"] != "collecting":
        await tg.toast(update, "That round is closed.", alert=True)
        return
    if not db.is_kid(user_id):
        await tg.toast(update, boards.kids_only(), alert=True)
        return
    for day in round_dates(rnd):
        db.x("INSERT INTO roster_availability (duty_date, user_id, available, round_id) "
             "VALUES (?, ?, 0, ?) ON CONFLICT(duty_date, user_id) DO UPDATE SET "
             "available = 0, round_id = excluded.round_id", (day, user_id, round_id))
    db.x("INSERT OR IGNORE INTO roster_responses (round_id, user_id) VALUES (?, ?)",
         (round_id, user_id))
    await tg.toast(update, "Noted — none of those work for you.")
    schedule_render(bot, round_id)
    await _maybe_close(bot, round_id)


async def _maybe_close(bot: Bot, round_id: int) -> None:
    """No point waiting out 48h once all three have answered (spec 7.2.3).

    But "answered" is not "finished": these are multi-select boards, and the
    third kid's FIRST tap would otherwise close the round while they are
    still ticking the rest of their Sundays. So the close runs on a short
    grace timer that every further tap pushes back.
    """
    kids = db.kids()
    if not kids:
        return
    answered = int(db.scalar("SELECT COUNT(*) FROM roster_responses WHERE round_id = ?",
                             (round_id,), default=0))
    if answered < len(kids):
        return
    grace = int(db.get_setting("roster_close_grace_seconds"))
    db.cancel_jobs("roster_close", round_id)
    db.add_job(t.now_utc() + timedelta(seconds=grace), "roster_close", round_id,
               respect_quiet=False)


async def _take(update, bot, day: str, user_id: int) -> None:
    if not db.is_kid(user_id):
        await tg.toast(update, boards.kids_only(), alert=True)
        return
    cur = db.x("UPDATE roster_slots SET assignee = ?, status = 'assigned' "
               "WHERE duty_date = ? AND assignee IS NULL", (user_id, day))
    if cur.rowcount == 0:
        slot = db.q1("SELECT * FROM roster_slots WHERE duty_date = ?", (day,))
        who = db.name_of(slot["assignee"]) if slot else "Someone"
        await tg.toast(update, boards.roster_already_assigned(who), alert=True)
        return
    db.log_duty(user_id, day, "roster", None)
    schedule_slot_reminders(day)
    await tg.toast(update, "Thanks! You're on 🙌")
    # Good news needs no ping: the box just updates where it sits.
    await render_box(bot, headline=boards.roster_taken(db.name_of(user_id), day))


async def _swap(update, bot, day: str, user_id: int) -> None:
    slot = db.q1("SELECT * FROM roster_slots WHERE duty_date = ?", (day,))
    if slot is None or slot["assignee"] != user_id:
        await tg.toast(update, boards.roster_not_yours(), alert=True)
        return
    clear_assignee(day)
    await tg.toast(update, "OK, I've asked the others.")
    # The box moves so the others see it; the freed Sunday carries a claim button.
    await render_box(bot, bump=True,
                     headline=boards.roster_swap_open(db.name_of(user_id), day))


# --- scheduled jobs --------------------------------------------------------

@scheduler.on("roster_nag")
async def _job_nag(bot: Bot, job) -> None:
    """Chase the silent kids by moving the box, not by adding to the chat."""
    rnd = db.q1("SELECT * FROM roster_rounds WHERE id = ?", (job["ref_id"],))
    if rnd is None or rnd["status"] != "collecting":
        return
    if round_state(rnd)["waiting"]:
        await render_box(bot, bump=True)
    db.add_job(t.now_utc() + timedelta(hours=db.get_int("roster_nag_hours")),
               "roster_nag", rnd["id"])


@scheduler.on("roster_deadline")
async def _job_deadline(bot: Bot, job) -> None:
    await close_round(bot, job["ref_id"])


@scheduler.on("roster_close")
async def _job_close(bot: Bot, job) -> None:
    """Everyone answered and the grace period elapsed - share them out."""
    await tg.debouncer.flush()
    await close_round(bot, job["ref_id"])


@scheduler.on("roster_remind")
async def _job_remind(bot: Bot, job) -> None:
    slot = db.q1("SELECT * FROM roster_slots WHERE id = ?", (job["ref_id"],))
    if slot is None or slot["assignee"] is None:
        return
    member = db.member_by_user(slot["assignee"])
    if member is None:
        return
    await render_box(bot, bump=True, headline=boards.roster_reminder(
        slot["duty_date"], tg.mention_member(member),
        (job["payload"] or "sat") == "sat"))
