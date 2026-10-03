"""Every word the family ever sees, and the keyboards that go with them.

Spec 9.4: keep all user-facing copy here so tone can be tuned in one file.
Each function takes plain data and returns (text, InlineKeyboardMarkup).
Tone: short, warm, plain English. Emoji as visual anchors, never as decoration.
"""
from __future__ import annotations

from datetime import timedelta

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M,
                      ReplyKeyboardRemove)

from app import timeutil as t
from app.tg import cb, esc, plain_list

# --- the four things Fambot does, and the command for each ----------------
# Labels are display only now; nothing is matched against message text.
L_DINNER = "🍜 Dinner"
L_RUSH = "🐕 Rush"
L_ROSTER = "📅 Roster"
L_HELP = "❓ Help"
C_DINNER = "/dinner"
C_RUSH = "/rush"
C_ROSTER = "/roster"
C_HELP = "/help"

TASK_LABELS = {"dogs": "watch the dogs", "house": "watch the house",
               "both": "watch the dogs and the house"}
KIND_LABELS = {"dog_accompany": "Come along on the walk",
               "dog_takeover": "Take over the walk (driver needed)",
               "coverage": "Be at home"}


def remove_keyboard() -> ReplyKeyboardRemove:
    """Clear the old reply keyboard off everyone's screen.

    is_persistent=False was not enough - the board still opened over the
    message box. Fambot is command-driven now, so there is no board at all.
    Sent once after the switch, and again on /setup and /start so clients
    that still hold the old one drop it.
    """
    return ReplyKeyboardRemove()


def home() -> tuple[str, M]:
    return ("👋 <b>Fambot</b>\nWhat do you need?",
            M([[B(L_DINNER, callback_data=cb("d", "menu")),
                B(L_RUSH, callback_data=cb("h", "menu"))],
               [B(L_ROSTER, callback_data=cb("r", "menu")),
                B(L_HELP, callback_data=cb("x", "help"))]]))


def help_text() -> str:
    return (
        "❓ <b>How Fambot works</b>\n\n"
        "Type <b>/</b> in the message box and Telegram lists these for you. "
        "Tap one, and everything after that is buttons.\n\n"
        f"<code>{C_DINNER}</code> {L_DINNER} — start a vote for the next family "
        "dinner. Take the next 7 days, or choose your own first and last day "
        "and every day in between goes on the vote. Tap every day you're "
        "free. If a day suits all five of us, "
        "anyone can lock it in; otherwise the admin picks the day that works "
        "for the most people. Run it again any time — it moves the same "
        "dinner box down to you rather than posting another one.\n\n"
        f"<code>{C_RUSH}</code> {L_RUSH} — ask for help with Rush. Pick a day and "
        "a time, say whether you want company or someone to take the walk over, "
        "and it goes to the group.\n\n"
        f"<code>{C_ROSTER}</code> {L_ROSTER} — see who's on Sunday duty, ask "
        "someone to be home, or set up the Sunday roster.\n\n"
        f"<code>{C_HELP}</code> {L_HELP} — this message."
    )


def not_set_up() -> str:
    return ("Fambot isn't set up yet. Someone needs to send <code>/setup</code> "
            "in the family group first.")


def not_registered() -> str:
    return "I don't know who you are yet — tap your name on the setup message first 🙂"


def wrong_chat() -> str:
    return "Fambot works in the family group, not here."


# --- setup / registration -------------------------------------------------

def registration(members) -> tuple[str, M]:
    lines = ["👋 <b>Hello family!</b>", "", "Tap your own name so I know who's who:"]
    rows, row = [], []
    for m in members:
        tick = "✅ " if m["user_id"] is not None else ""
        row.append(B(f"{tick}{m['name']}", callback_data=cb("s", "reg", m["slug"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    done = [m["name"] for m in members if m["user_id"] is not None]
    todo = [m["name"] for m in members if m["user_id"] is None]
    if done:
        lines += ["", "✅ Registered: " + plain_list(done)]
    if todo:
        lines += ["⏳ Still waiting on: " + plain_list(todo)]
    else:
        lines += ["", "Everyone's in. You're ready to go 🎉"]
    return "\n".join(lines), M(rows)


def setup_done() -> str:
    return ("✅ Fambot is set up for this group.\n\n"
            "Type <b>/</b> in the message box to see everything I can do — "
            f"<code>{C_HELP}</code> any time for a reminder.")


def switched_to_commands() -> str:
    """One-off notice when the old bottom-of-screen keyboard is taken away."""
    return ("🔁 <b>Fambot changed slightly.</b>\n\n"
            "The four buttons that sat on top of your keyboard are gone — they "
            "were in the way. Type <b>/</b> in the message box instead and pick "
            f"from the list. <code>{C_HELP}</code> explains the rest.")


def whoami(name: str | None, user_id: int) -> str:
    who = f"You're registered as <b>{esc(name)}</b>.\n" if name else "You're not registered yet.\n"
    return f"{who}Your Telegram ID is <code>{user_id}</code>."


# --- Feature A: dinner ----------------------------------------------------
# One box, always. Every dinner state - vote, confirm, locked, reminder,
# cancelled, done - is the same single message being rewritten. The only time
# a new message appears is when something must actually reach phones, and
# then the old one is taken down first (see dinner.py).

def dinner_board_moved() -> str:
    """Left in place of a board the bot was not allowed to delete.

    Without group-admin rights Telegram only lets the bot remove its own
    messages for 48 hours. Past that we at least strip the board of its
    buttons so nobody taps a dead one.
    """
    return "🍜 <i>Dinner moved to the bottom of the chat ⬇</i>"


def dinner_when(first, days: int) -> tuple[str, M]:
    """The first thing /dinner shows: which dates go on the vote.

    `first` is the earliest day a dinner can be (tomorrow). The top button is
    the old behaviour - the next `days` days - so the common case stays one tap.
    """
    last = first + timedelta(days=days - 1)
    return ("🍜 <b>FAMILY DINNER</b>\nWhich days should we vote on?",
            M([[B(f"Next {days} days ({t.fmt_date(first)} – {t.fmt_date(last)})",
                  callback_data=cb("d", "go", first.isoformat(), last.isoformat()))],
               [B("📆 Choose the dates", callback_data=cb("d", "ps", 0))],
               [B("✖ Never mind", callback_data=cb("d", "pc"))]]))


def _date_grid(dates: list, token) -> list[list[B]]:
    rows, row = [], []
    for dd in dates:
        row.append(B(t.fmt_date(dd), callback_data=token(dd)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def dinner_pick_start(dates: list, page: int, pages: int) -> tuple[str, M]:
    rows = _date_grid(dates, lambda dd: cb("d", "pe", dd.isoformat(), 0))
    nav = []
    if page > 0:
        nav.append(B("⬅ Earlier", callback_data=cb("d", "ps", page - 1)))
    if page < pages - 1:
        nav.append(B("Later ➡", callback_data=cb("d", "ps", page + 1)))
    if nav:
        rows.append(nav)
    rows.append([B("⬅ Back", callback_data=cb("d", "pm")),
                 B("✖ Never mind", callback_data=cb("d", "pc"))])
    return ("🍜 <b>FAMILY DINNER</b>\n📆 Step 1 of 2 — tap the <b>first</b> day "
            "to vote on.", M(rows))


def dinner_pick_end(start: str, dates: list, page: int, pages: int) -> tuple[str, M]:
    """`dates` begins at `start` itself on page 0, so one tap can mean one day."""
    rows = _date_grid(dates, lambda dd: cb("d", "go", start, dd.isoformat()))
    nav = []
    if page > 0:
        nav.append(B("⬅ Earlier", callback_data=cb("d", "pe", start, page - 1)))
    if page < pages - 1:
        nav.append(B("Later ➡", callback_data=cb("d", "pe", start, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([B("⬅ Back", callback_data=cb("d", "ps", 0)),
                 B("✖ Never mind", callback_data=cb("d", "pc"))])
    return (f"🍜 <b>FAMILY DINNER</b>\n📆 Step 2 of 2 — starting "
            f"<b>{t.fmt_date(start)}</b>. Now tap the <b>last</b> day.\n"
            "Every day in between goes on the vote.", M(rows))


def dinner_dates_stale() -> str:
    return "Those dates don't work any more — pick again."


def dinner_already_running() -> str:
    return "There's already a dinner on the go."


def dinner_poll(poll_id: int, days: list[dict], voted: list[str], waiting: str,
                deadline, missing: list[str]) -> tuple[str, M]:
    """The live vote board.

    `days` items: {date, count, required, names, viable}. `waiting` arrives as
    ready-made mention HTML, not names: when this board is re-posted to chase
    non-voters the mentions are what actually pings them, so the nag needs no
    message of its own.

    Buttons show the aggregate tally rather than a personal tick - an inline
    keyboard is shared by everyone in the group, so it cannot show one
    person's own state. Who picked what is spelled out in the text instead,
    and the tapper gets a private toast confirming their own toggle.
    """
    lines = ["🍜 <b>FAMILY DINNER VOTE</b>",
             "Tap every day you can make dinner. Tap again to undo.", ""]
    for day in days:
        star = "⭐ " if day["viable"] else ""
        who = ", ".join(esc(n) for n in day["names"])
        tail = f" — {who}" if who else ""
        if day["viable"]:
            tail = " — everyone's free!"
        lines.append(f"{star}<b>{t.fmt_date(day['date'])}</b> · "
                     f"{day['count']}/{day['required']}{tail}")
    lines.append("")
    if voted:
        lines.append("✅ Voted: " + plain_list([esc(n) for n in voted]))
    if waiting:
        lines.append("⏳ Waiting on: " + waiting)
    if missing:
        lines.append("⚠️ Not registered yet, so we can't reach 5/5: "
                     + plain_list([esc(n) for n in missing]))
    lines += ["", f"<i>Voting closes {t.fmt_datetime(deadline)}.</i>"]

    btns = [B(f"{'⭐ ' if d['viable'] else ''}{t.fmt_date(d['date'])}  ·  "
              f"{d['count']}/{d['required']}",
              callback_data=cb("d", "v", poll_id, d["date"])) for d in days]
    # A chosen range can run to a month; two to a row keeps that on one screen.
    per_row = 1 if len(btns) <= 7 else 2
    rows = [btns[i:i + per_row] for i in range(0, len(btns), per_row)]
    rows.append([B("🙅 No days work for me", callback_data=cb("d", "none", poll_id))])
    for day in days:
        if day["viable"]:
            rows.append([B(f"🔒 Lock in {t.fmt_date(day['date'])}",
                           callback_data=cb("d", "lock", poll_id, day["date"]))])
    rows.append([B("✖ Cancel this vote", callback_data=cb("d", "kill", poll_id))])
    return "\n".join(lines), M(rows)


def dinner_lock_confirm(poll_id: int, day: str) -> tuple[str, M]:
    return (f"Lock dinner in for <b>{t.fmt_date(day)}</b>?",
            M([[B("✅ Yes, lock it", callback_data=cb("d", "lockc", poll_id, day)),
                B("⬅ Back", callback_data=cb("d", "board", poll_id))]]))


def dinner_finalize(poll_id: int, ranked: list[dict], admin_name: str,
                    closed: bool) -> tuple[str, M]:
    """Admin-only prompt to pick the day.

    Shown once everyone has voted, and again when voting time runs out - in
    both cases the admin can take any day with at least one vote, so "four of
    us can make Friday" is a bookable outcome rather than a dead end.
    `ranked` items: {date, count, required, names}, already sorted best-first.
    """
    head = ("🍜 <b>Time's up on the dinner vote.</b>" if closed
            else "🍜 <b>Everyone's voted — pick the dinner day.</b>")
    lines = [head, "",
             f"{esc(admin_name)} can lock in any day below (top one works for "
             "the most people):", ""]
    for d in ranked:
        who = plain_list([esc(n) for n in d["names"]]) or "nobody"
        lines.append(f"<b>{t.fmt_date(d['date'])}</b> · {d['count']}/{d['required']} "
                     f"— {who}")
    rows = [[B(f"🔒 {t.fmt_date(d['date'])} ({d['count']}/{d['required']})",
               callback_data=cb("d", "fin", poll_id, d["date"]))]
            for d in ranked]
    rows.append([B("✖ Cancel this vote", callback_data=cb("d", "kill", poll_id))])
    return "\n".join(lines), M(rows)


def dinner_finalize_none(poll_id: int, days: int, closed: bool) -> tuple[str, M]:
    head = ("😔 <b>Voting's closed and nobody picked a day.</b>" if closed
            else "😔 <b>Everyone's voted, but nobody can make any day.</b>")
    nxt = "day" if days == 1 else f"{days} days"
    return (f"{head}\n\nTry the next stretch of days?",
            M([[B(f"🔁 Try the next {nxt}", callback_data=cb("d", "ext", poll_id))],
               [B("✖ Drop it for now", callback_data=cb("d", "drop", poll_id))]]))


def dinner_not_admin(admin_name: str) -> str:
    return f"Only {admin_name} can change the dinner."


# Headline per stage of a locked dinner. The same box carries all of them.
_DINNER_HEADS = {
    "locked": "🍜 <b>DINNER IS ON — {day}</b>",
    "t3": "🍜 <b>Family dinner in 3 days — {day}</b>",
    "t1": "🍜 <b>Family dinner is tomorrow — {day}</b>",
    "day": "🍜 <b>Family dinner is tonight — {day}</b>",
}


def dinner_event_card(event_id: int, day: str, coming: str, absent: list[str],
                      phase: str, locked_by: str | None = None) -> tuple[str, M]:
    """The one box for a locked-in dinner, from lock right through to the day.

    `coming` is mention HTML for the people who voted for this date, so every
    time the card is re-posted as a reminder it pings exactly them and nobody
    else. Nobody is asked to re-confirm: the guest list was settled by the
    vote, and only the admin changes the plan after that.
    """
    lines = [_DINNER_HEADS[phase].format(day=t.fmt_date(day)), ""]
    lines.append("🙋 Coming: " + coming if coming
                 else "🙋 Nobody ticked this day, so I don't know who's coming.")
    if absent:
        lines.append("😴 Not coming: " + plain_list([esc(n) for n in absent]))
    if phase == "locked":
        who = f"{esc(locked_by)} locked it in. " if locked_by else ""
        lines += ["", f"<i>{who}I'll bump this up again nearer the day.</i>"]
    rows = [[B("🔁 Pick another day", callback_data=cb("d", "revote", event_id)),
             B("✖ Cancel dinner", callback_data=cb("d", "ecancel", event_id))]]
    return "\n".join(lines), M(rows)


def dinner_event_cancelled(day: str) -> tuple[str, M]:
    return (f"✖ <b>Dinner on {t.fmt_date(day)} is off.</b>",
            M([[B("🍜 Start a new vote", callback_data=cb("d", "open"))]]))


def dinner_event_done(day: str) -> tuple[str, M]:
    return (f"🥢 <i>Family dinner on {t.fmt_date(day)} — done. Hope it was good!</i>",
            M([]))


def dinner_vote_cancelled() -> tuple[str, M]:
    return ("✖ <i>Dinner vote cancelled.</i>\n"
            f"<code>{C_DINNER}</code> whenever you want to try again.", M([]))


# --- Feature B: help requests ---------------------------------------------

def dog_menu(open_count: int) -> tuple[str, M]:
    rows = [[B("🆘 I need help with Rush", callback_data=cb("h", "new", "dog"))]]
    if open_count:
        rows.append([B(f"👀 See open requests ({open_count})",
                       callback_data=cb("h", "list"))])
    else:
        rows.append([B("👀 See open requests", callback_data=cb("h", "list"))])
    return "🐕 <b>Rush</b>\nWhat do you need?", M(rows)


def pick_date(flow: str, dates: list, page: int, pages: int) -> tuple[str, M]:
    rows, row = [], []
    for dd in dates:
        row.append(B(t.fmt_date_rel(dd), callback_data=cb("h", "d", flow, dd.isoformat())))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(B("⬅ Earlier", callback_data=cb("h", "page", flow, page - 1)))
    if page < pages - 1:
        nav.append(B("Later ➡", callback_data=cb("h", "page", flow, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([B("✖ Cancel", callback_data=cb("x", "close"))])
    return "📆 Which day?", M(rows)


def quick_date(flow: str) -> tuple[str, M]:
    today = t.today_local()
    tmr = today + timedelta(days=1)
    return ("📆 Which day?",
            M([[B("Today", callback_data=cb("h", "d", flow, today.isoformat())),
                B("Tomorrow", callback_data=cb("h", "d", flow, tmr.isoformat()))],
               [B("📆 Pick a day", callback_data=cb("h", "page", flow, 0))],
               [B("✖ Cancel", callback_data=cb("x", "close"))]]))


def pick_window(flow: str, day: str) -> tuple[str, M]:
    rows = [[B(f"{icon} {t.window_blurb(w)}", callback_data=cb("h", "w", flow, day, w))]
            for w, icon in (("morning", "🌅"), ("afternoon", "☀️"), ("evening", "🌆"))]
    rows.append([B("⬅ Back", callback_data=cb("h", "new", flow)),
                 B("✖ Cancel", callback_data=cb("x", "close"))])
    return f"🕑 What time on {t.fmt_date(day)}?", M(rows)


def pick_dog_type(day: str, window: str) -> tuple[str, M]:
    return (f"🐕 {t.fmt_date(day)}, {t.window_label(window)} — what kind of help?",
            M([[B("👥 Come along with me",
                  callback_data=cb("h", "mk", "dog_accompany", day, window))],
               [B("🚗 Take over the walk",
                  callback_data=cb("h", "mk", "dog_takeover", day, window))],
               [B("⬅ Back", callback_data=cb("h", "d", "dog", day)),
                B("✖ Cancel", callback_data=cb("x", "close"))]]))


def pick_cover_task(day: str, window: str) -> tuple[str, M]:
    return (f"🏠 {t.fmt_date(day)}, {t.window_label(window)} — what needs covering?",
            M([[B("🐕 Watch the dogs",
                  callback_data=cb("h", "mk", "coverage", day, window, "dogs"))],
               [B("🏠 Watch the house",
                  callback_data=cb("h", "mk", "coverage", day, window, "house"))],
               [B("🐕🏠 Both",
                  callback_data=cb("h", "mk", "coverage", day, window, "both"))],
               [B("⬅ Back", callback_data=cb("h", "d", "cov", day)),
                B("✖ Cancel", callback_data=cb("x", "close"))]]))


def help_request(req, requester: str, claimant: str | None,
                 viewer_is_requester: bool = False) -> tuple[str, M]:
    kind, rid = req["kind"], req["id"]
    if kind == "coverage":
        head = (f"🏠 <b>{esc(requester)} needs someone home</b>\n"
                f"📆 {t.fmt_date(req['req_date'])}, {t.window_blurb(req['time_window'])}\n"
                f"Job: {TASK_LABELS.get(req['task_label'], 'be at home')}")
    else:
        head = (f"🐕 <b>{esc(requester)} needs help with Rush</b>\n"
                f"📆 {t.fmt_date(req['req_date'])}, {t.window_blurb(req['time_window'])}\n"
                f"Job: {KIND_LABELS[kind]}")

    status, rows = req["status"], []
    if status == "open":
        rows = [[B("🙋 I'll do it", callback_data=cb("h", "claim", rid))],
                [B("✖ Cancel this", callback_data=cb("h", "kill", rid))]]
        body = f"{head}\n\n<i>Nobody's picked this up yet.</i>"
    elif status == "claimed":
        body = f"{head}\n\n✅ <b>{esc(claimant)}'s got it — thanks!</b>"
        rows = [[B("❌ I can't do it any more", callback_data=cb("h", "drop", rid))],
                [B("✖ Cancel this", callback_data=cb("h", "kill", rid))]]
    elif status == "cancelled":
        body = f"{head}\n\n✖ <i>Cancelled.</i>"
    elif status == "expired":
        body = f"{head}\n\n⌛ <i>Nobody was free. This one's closed.</i>"
    else:
        body = f"{head}\n\n✅ <i>Done.</i>"
    return body, M(rows)


def help_posted() -> str:
    return "✅ Posted to the group."


def help_needs_driver() -> str:
    return "This one needs a driver 🚗"


def help_already_claimed(who: str) -> str:
    return f"{who} already grabbed this one 😄"


def help_claimed_toast() -> str:
    return "Thanks! You're on it 🙌"


def help_reping(mentions: str, requester: str, day: str, window: str, round_no: int) -> str:
    when = f"{t.fmt_date_rel(day).lower()} {t.window_label(window).lower()}"
    if round_no <= 1:
        return f"🙋 Anyone free to help {esc(requester)} {when}? {mentions}"
    if round_no == 2:
        return f"🙋 Still nobody for {esc(requester)} {when}. {mentions}?"
    return f"🥺 Still nobody free to help {esc(requester)} {when}. {mentions}"


def help_expired(requester_mention: str, day: str, window: str) -> str:
    return (f"⌛ Nobody could help with {t.fmt_date(day)}, "
            f"{t.window_label(window).lower()} — sorry {requester_mention}.")


def help_claimant_reminder(req, claimant_mention: str, requester: str,
                           soon: bool) -> tuple[str, M]:
    when = "in about an hour" if soon else f"tomorrow {t.window_label(req['time_window']).lower()}"
    if req["kind"] == "coverage":
        what = f"you're covering at home for {esc(requester)}"
    elif req["kind"] == "dog_takeover":
        what = f"you're taking Rush out for {esc(requester)}"
    else:
        what = f"you're going along with {esc(requester)} and Rush"
    return (f"🔔 {claimant_mention} — reminder, {what} {when}.",
            M([[B("👍 On it", callback_data=cb("h", "ack", req["id"])),
                B("❌ I can't any more", callback_data=cb("h", "drop", req["id"]))]]))


def help_unclaimed(who: str) -> str:
    return f"😔 {who} can't do it any more — this one's open again."


def help_list(reqs: list[dict]) -> tuple[str, M]:
    if not reqs:
        return "👀 Nothing open right now.", M([])
    lines = ["👀 <b>Open requests</b>", ""]
    rows = []
    for r in reqs:
        icon = "🏠" if r["kind"] == "coverage" else "🐕"
        lines.append(f"{icon} {t.fmt_date(r['req_date'])}, "
                     f"{t.window_label(r['time_window'])} — {esc(r['requester'])}")
        rows.append([B(f"🙋 Take {icon} {t.fmt_date(r['req_date'])} "
                       f"{t.window_label(r['time_window'])}",
                       callback_data=cb("h", "claim", r["id"]))])
    return "\n".join(lines), M(rows)


def help_cancelled() -> str:
    return "✖ Request cancelled."


def not_yours() -> str:
    return "Only the person who asked (or an admin) can do that."


# --- Feature C: roster ----------------------------------------------------

# One box, same as dinner. The roster is a single message: the availability
# vote while a round is running, the roster itself the rest of the time. It is
# edited silently for small changes and taken down and re-posted only when
# something has to reach phones (see roster.py).

def roster_board_moved() -> str:
    """Left in place of a roster box the bot was not allowed to delete."""
    return "📅 <i>Roster moved to the bottom of the chat ⬇</i>"


def roster_card(slots: list[dict], helps: list[dict], has_roster: bool,
                headline: str | None = None) -> tuple[str, M]:
    """The roster box when no round is running: who is on, and what to tap.

    `slots` items: {date, name, assignee} for every future Sunday on the
    books. `headline` is whatever just happened - a reminder, a swap request,
    the result of a round - and sits on top so the re-posted box says why it
    moved. An inline keyboard is shared by the whole group, so every held
    Sunday gets its own swap button; the handler turns away anyone who taps
    a Sunday that is not theirs.
    """
    lines = [headline, ""] if headline else []
    lines += ["📅 <b>Roster</b> — Sunday duty, evenings after 6pm", ""]
    rows = []
    covered = [s for s in slots if s["assignee"] is not None]
    unfilled = [s["date"] for s in slots if s["assignee"] is None]
    for s in slots:
        who = esc(s["name"]) if s["name"] else "⚠️ nobody yet"
        lines.append(f"<b>{t.fmt_date(s['date'])}</b> — {who}")
    if unfilled:
        lines += ["", "⚠️ No cover for " + plain_list([t.fmt_date(d) for d in unfilled])
                  + ". Anyone able to step in?"]
        rows += [[B(f"🙋 I'll take {t.fmt_date(d)}", callback_data=cb("r", "take", d))]
                 for d in unfilled]
    if covered:
        lines += ["", f"<i>Covered up to {t.fmt_date(covered[-1]['date'])}. "
                      "Tap ➕ to sort out the Sundays after that.</i>"]
        rows += [[B(f"🔁 {s['name']} can't do {t.fmt_date(s['date'])}",
                    callback_data=cb("r", "swap", s["date"]))] for s in covered]
    elif has_roster:
        lines += ["", "<i>No Sunday has anyone on it right now. Tap ➕ to sort "
                      "out the next few.</i>"]
    else:
        lines.append("No Sunday roster yet.")
    if helps:
        lines += ["", "<b>Help requests</b>"]
        for h in helps:
            icon = "🏠" if h["kind"] == "coverage" else "🐕"
            who = esc(h["claimant"]) if h["claimant"] else "⚠️ nobody yet"
            lines.append(f"  {icon} {t.fmt_date(h['req_date'])} "
                         f"{t.window_label(h['time_window'])} — {who}")
    rows.append([B("🏠 Ask someone to be home", callback_data=cb("h", "new", "cov"))])
    label = "⚙️ Set up the Sunday roster" if not has_roster else "➕ Plan more Sundays"
    rows.append([B(label, callback_data=cb("r", "setup"))])
    return "\n".join(lines), M(rows)


def roster_round(round_id: int, days: list[dict], responded: list[str],
                 waiting: str, deadline, covered: list[dict] | None = None,
                 headline: str | None = None) -> tuple[str, M]:
    """The roster box while an availability round is running.

    `waiting` arrives as ready-made mention HTML, as on the dinner board: when
    the box is re-posted to chase the kids who have not answered, the mentions
    are what pings them. `covered` items: {date, name} - Sundays already
    sorted, shown so it is obvious why they are not being asked about again.
    """
    lines = [headline, ""] if headline else []
    lines += ["📅 <b>SUNDAY DUTY — who's free?</b>",
              "Evenings after 6pm. Tap every Sunday you <b>can</b> do.", ""]
    if covered:
        lines += [f"✔ {t.fmt_date(c['date'])} — {esc(c['name'])} (already sorted)"
                  for c in covered]
        lines.append("")
    for day in days:
        who = ", ".join(esc(n) for n in day["names"]) or "nobody yet"
        lines.append(f"<b>{t.fmt_date(day['date'])}</b> — {who}")
    lines.append("")
    if responded:
        lines.append("✅ Answered: " + plain_list([esc(n) for n in responded]))
    if waiting:
        lines.append("⏳ Waiting on: " + waiting)
    lines += ["", f"<i>Closes {t.fmt_datetime(deadline)}, then I'll share it out fairly.</i>"]

    rows = [[B(f"{t.fmt_date(d['date'])}  ·  {d['count']} free",
               callback_data=cb("r", "av", round_id, d["date"]))] for d in days]
    rows.append([B("🙅 None of these work for me",
                   callback_data=cb("r", "none", round_id))])
    return "\n".join(lines), M(rows)


def roster_shared_out() -> str:
    return "✅ <b>Sundays shared out.</b>"


def roster_no_kids() -> str:
    return ("I need the kids registered before I can build a roster — "
            "tap your names on the setup message first.")


def roster_taken(who: str, day: str) -> str:
    return f"🙌 {esc(who)} has {t.fmt_date(day)} covered."


def roster_swap_open(who: str, day: str) -> str:
    return (f"🔁 <b>{esc(who)} can't do Sunday duty on {t.fmt_date(day)}.</b> "
            "Anyone able to swap in?")


def roster_reminder(day: str, mention_text: str, tomorrow: bool) -> str:
    when = "tomorrow evening" if tomorrow else "this evening"
    return f"🔔 {mention_text} — you're on home duty {when} ({t.fmt_date(day)}), after 6pm."


def roster_not_yours() -> str:
    return "That's not your Sunday 🙂"


def roster_already_assigned(who: str) -> str:
    return f"{who} already has that one."


def roster_round_open() -> str:
    return "There's already a Sunday availability round running 📅"


def kids_only() -> str:
    return "This one's for the three of you kids 🙂"
