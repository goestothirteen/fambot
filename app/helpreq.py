"""Feature B - help requests.

One engine, two skins (spec 6). A request is (requester, kind, date, window,
claim rules); everything after that - posting, re-pinging, claiming,
reminding, un-claiming - is shared code. The dog-walk and home-coverage
entry points differ only in which questions they ask on the way in.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app import boards, db, scheduler, tg
from app import timeutil as t

log = logging.getLogger(__name__)

PAGE = 7          # days per page in the "pick a day" picker
PAGES = 2         # 14 days of runway, per spec 6.1


# --- queries ---------------------------------------------------------------

def open_requests() -> list:
    return db.q("SELECT * FROM help_requests WHERE status = 'open' "
                "ORDER BY req_date, time_window")


def active_requests(days: int = 14) -> list:
    until = (t.today_local() + timedelta(days=days)).isoformat()
    return db.q("SELECT * FROM help_requests WHERE status IN ('open','claimed') "
                "AND req_date <= ? ORDER BY req_date, time_window", (until,))


def can_claim(req, user_id: int) -> tuple[bool, str]:
    """Who is allowed to take this on.

    Taking the walk over means driving, and Mark is the only other driver -
    so that button is driver-only. Everything else is open to any kid, and
    a parent who wants to pitch in is not blocked (spec 3).
    """
    if req["kind"] == "dog_takeover" and not db.is_driver(user_id):
        return False, boards.help_needs_driver()
    if req["requested_by"] == user_id:
        return False, "You're the one who asked 🙂"
    return True, ""


# --- rendering -------------------------------------------------------------

async def render(bot: Bot, req_id: int) -> None:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (req_id,))
    if req is None:
        return
    chat_id = db.group_chat_id()
    if chat_id is None:
        return
    text, markup = boards.help_request(
        req, db.name_of(req["requested_by"]),
        db.name_of(req["claimed_by"]) if req["claimed_by"] else None)
    new_id = await tg.edit_or_repost(bot, chat_id, req["message_id"], text, markup)
    if new_id and new_id != req["message_id"]:
        db.x("UPDATE help_requests SET message_id = ? WHERE id = ?", (new_id, req_id))


# --- creating --------------------------------------------------------------

async def create(bot: Bot, kind: str, requester: int, day: str, window: str,
                 task_label: str | None = None) -> int:
    cur = db.x(
        "INSERT INTO help_requests (kind, task_label, requested_by, req_date, "
        "time_window, status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (kind, task_label, requester, day, window, t.iso(t.now_utc())))
    req_id = int(cur.lastrowid)

    start, _ = t.window_bounds(day, window)
    first = t.now_utc() + timedelta(hours=db.get_int("help_first_reping_hours"))
    if first < start:
        db.add_job(first, "help_reping", req_id, "1")
    # Nagging stops when the window itself starts; then it is simply too late.
    db.add_job(start, "help_expire", req_id, respect_quiet=False)
    await render(bot, req_id)
    return req_id


def schedule_claimant_reminders(req_id: int, day: str, window: str) -> None:
    db.cancel_jobs("help_remind", req_id)
    start, _ = t.window_bounds(day, window)
    now = t.now_utc()
    evening_before = t.local_at(t.parse_date(day) - timedelta(days=1),
                                db.get_int("help_claimant_reminder_hour"))
    if now < evening_before < start:
        db.add_job(evening_before, "help_remind", req_id, "before")
    hour_before = start - timedelta(hours=1)
    if hour_before > now:
        db.add_job(hour_before, "help_remind", req_id, "soon", respect_quiet=False)


# --- claim / unclaim -------------------------------------------------------

def claim(req_id: int, user_id: int) -> bool:
    """First valid tap wins.

    The WHERE clause is the lock: two people tapping in the same instant
    produce one UPDATE with rowcount 1 and one with rowcount 0.
    """
    cur = db.x("UPDATE help_requests SET status = 'claimed', claimed_by = ?, "
               "claimed_at = ? WHERE id = ? AND status = 'open'",
               (user_id, t.iso(t.now_utc()), req_id))
    if cur.rowcount == 0:
        return False
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (req_id,))
    db.log_duty(user_id, req["req_date"], req["kind"], req_id)
    db.cancel_jobs("help_reping", req_id)
    schedule_claimant_reminders(req_id, req["req_date"], req["time_window"])
    return True


def unclaim(req_id: int, user_id: int) -> bool:
    cur = db.x("UPDATE help_requests SET status = 'open', claimed_by = NULL, "
               "claimed_at = NULL WHERE id = ? AND status = 'claimed' AND claimed_by = ?",
               (req_id, user_id))
    if cur.rowcount == 0:
        return False
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (req_id,))
    db.unlog_duty(req["kind"], req_id, user_id)
    db.cancel_jobs("help_remind", req_id)
    start, _ = t.window_bounds(req["req_date"], req["time_window"])
    if start > t.now_utc():
        db.add_job(t.now_utc() + timedelta(minutes=1), "help_reping", req_id, "1")
    return True


def cancel(req_id: int, user_id: int) -> tuple[bool, str]:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (req_id,))
    if req is None or req["status"] in ("cancelled", "expired", "done"):
        return False, "That one's already closed."
    if req["requested_by"] != user_id and not db.is_admin(user_id):
        return False, boards.not_yours()
    db.x("UPDATE help_requests SET status = 'cancelled' WHERE id = ?", (req_id,))
    db.unlog_duty(req["kind"], req_id)
    db.cancel_jobs("help_reping", req_id)
    db.cancel_jobs("help_expire", req_id)
    db.cancel_jobs("help_remind", req_id)
    return True, ""


# --- entry points ----------------------------------------------------------

async def entry_dog(bot: Bot) -> None:
    text, markup = boards.dog_menu(len(open_requests()))
    await tg.send_group(bot, text, markup)


async def entry_coverage(bot: Bot) -> None:
    text, markup = boards.quick_date("cov")
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
        await entry_dog(bot)

    elif action == "new":
        await tg.toast(update, "")
        flow = parts[2]
        text, markup = boards.quick_date(flow)
        await tg.send_group(bot, text, markup)

    elif action == "page":
        await tg.toast(update, "")
        flow, page = parts[2], int(parts[3])
        start = t.today_local() + timedelta(days=page * PAGE)
        dates = [start + timedelta(days=i) for i in range(PAGE)]
        text, markup = boards.pick_date(flow, dates, page, PAGES)
        await tg.send_group(bot, text, markup)

    elif action == "d":
        await tg.toast(update, "")
        flow, day = parts[2], parts[3]
        text, markup = boards.pick_window(flow, day)
        await tg.send_group(bot, text, markup)

    elif action == "w":
        await tg.toast(update, "")
        flow, day, window = parts[2], parts[3], parts[4]
        if flow == "dog":
            text, markup = boards.pick_dog_type(day, window)
        else:
            text, markup = boards.pick_cover_task(day, window)
        await tg.send_group(bot, text, markup)

    elif action == "mk":
        kind, day, window = parts[2], parts[3], parts[4]
        label = parts[5] if len(parts) > 5 else None
        await tg.toast(update, boards.help_posted())
        await create(bot, kind, user.id, day, window, label)

    elif action == "claim":
        await _claim(update, bot, int(parts[2]), user.id)

    elif action == "drop":
        req_id = int(parts[2])
        if unclaim(req_id, user.id):
            await tg.toast(update, "OK, I've reopened it.")
            await render(bot, req_id)
            await tg.send_group(bot, boards.help_unclaimed(db.name_of(user.id)))
        else:
            await tg.toast(update, "You're not the one signed up for that 🙂", alert=True)

    elif action == "ack":
        await tg.toast(update, "👍 Thanks!")

    elif action == "kill":
        ok, msg = cancel(int(parts[2]), user.id)
        if ok:
            await tg.toast(update, "")
            await render(bot, int(parts[2]))
            await tg.send_group(bot, boards.help_cancelled())
        else:
            await tg.toast(update, msg, alert=True)

    elif action == "list":
        await tg.toast(update, "")
        reqs = [{"id": r["id"], "kind": r["kind"], "req_date": r["req_date"],
                 "time_window": r["time_window"],
                 "requester": db.name_of(r["requested_by"])} for r in open_requests()]
        text, markup = boards.help_list(reqs)
        await tg.send_group(bot, text, markup)


async def _claim(update, bot, req_id: int, user_id: int) -> None:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (req_id,))
    if req is None:
        return
    if req["status"] != "open":
        who = db.name_of(req["claimed_by"]) if req["claimed_by"] else "Someone"
        await tg.toast(update, boards.help_already_claimed(who), alert=True)
        return
    allowed, why = can_claim(req, user_id)
    if not allowed:
        await tg.toast(update, why, alert=True)
        return
    if claim(req_id, user_id):
        await tg.toast(update, boards.help_claimed_toast())
        await render(bot, req_id)
    else:
        await tg.toast(update, boards.help_already_claimed("Someone"), alert=True)


# --- scheduled jobs --------------------------------------------------------

@scheduler.on("help_reping")
async def _job_reping(bot: Bot, job) -> None:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (job["ref_id"],))
    if req is None or req["status"] != "open":
        return
    start, _ = t.window_bounds(req["req_date"], req["time_window"])
    if t.now_utc() >= start:
        return

    round_no = int(job["payload"] or "1")
    # Target the kids, but a parent tapping the button is never blocked.
    targets = [m for m in db.kids() if m["user_id"] != req["requested_by"]]
    if req["kind"] == "dog_takeover":
        targets = [m for m in targets if m["is_driver"]]
    if targets:
        await tg.send_group(bot, boards.help_reping(
            tg.mention_list(targets), db.name_of(req["requested_by"]),
            req["req_date"], req["time_window"], round_no))

    nxt = t.now_utc() + timedelta(hours=db.get_int("help_reping_hours"))
    if nxt < start:
        db.add_job(nxt, "help_reping", req["id"], str(round_no + 1))


@scheduler.on("help_expire")
async def _job_expire(bot: Bot, job) -> None:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (job["ref_id"],))
    if req is None or req["status"] != "open":
        return
    db.x("UPDATE help_requests SET status = 'expired' WHERE id = ?", (req["id"],))
    db.cancel_jobs("help_reping", req["id"])
    await render(bot, req["id"])
    requester = db.member_by_user(req["requested_by"])
    await tg.send_group(bot, boards.help_expired(
        tg.mention_member(requester) if requester else "everyone",
        req["req_date"], req["time_window"]))


@scheduler.on("help_remind")
async def _job_remind(bot: Bot, job) -> None:
    req = db.q1("SELECT * FROM help_requests WHERE id = ?", (job["ref_id"],))
    if req is None or req["status"] != "claimed":
        return
    claimant = db.member_by_user(req["claimed_by"])
    if claimant is None:
        return
    text, markup = boards.help_claimant_reminder(
        req, tg.mention_member(claimant), db.name_of(req["requested_by"]),
        (job["payload"] or "before") == "soon")
    await tg.send_group(bot, text, markup)
