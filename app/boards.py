"""Every word the family ever sees, and the keyboards that go with them.

Spec 9.4: keep all user-facing copy here so tone can be tuned in one file.
Each function takes plain data and returns (text, InlineKeyboardMarkup).
Tone: short, warm, plain English. Emoji as visual anchors, never as decoration.
"""
from __future__ import annotations

from datetime import timedelta

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M,
                      KeyboardButton, ReplyKeyboardMarkup)

from app import timeutil as t
from app.tg import cb, esc, plain_list

# --- the four fixed labels on the persistent keyboard ---------------------
L_DINNER = "🍜 Dinner"
L_RUSH = "🐕 Rush"
L_ROSTER = "📅 Roster"
L_HELP = "❓ Help"
LABELS = {L_DINNER, L_RUSH, L_ROSTER, L_HELP}

TASK_LABELS = {"dogs": "watch the dogs", "house": "watch the house",
               "both": "watch the dogs and the house"}
KIND_LABELS = {"dog_accompany": "Come along on the walk",
               "dog_takeover": "Take over the walk (driver needed)",
               "coverage": "Be at home"}


def reply_keyboard() -> ReplyKeyboardMarkup:
    """The board. Always at the bottom of the screen, so nobody types anything."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(L_DINNER), KeyboardButton(L_RUSH)],
         [KeyboardButton(L_ROSTER), KeyboardButton(L_HELP)]],
        resize_keyboard=True, is_persistent=True)


def home() -> tuple[str, M]:
    return ("👋 <b>Fambot</b>\nWhat do you need?",
            M([[B(L_DINNER, callback_data=cb("d", "menu")),
                B(L_RUSH, callback_data=cb("h", "menu"))],
               [B(L_ROSTER, callback_data=cb("r", "menu")),
                B(L_HELP, callback_data=cb("x", "help"))]]))


def help_text() -> str:
    return (
        "❓ <b>How Fambot works</b>\n\n"
        f"{L_DINNER} — start a vote for the next family dinner. Tap every day "
        "you're free; when all five of us are free on the same day, anyone can "
        "lock it in.\n\n"
        f"{L_RUSH} — ask for help with Rush. Pick a day and a time, say whether "
        "you want company or someone to take the walk over, and it goes to the group.\n\n"
        f"{L_ROSTER} — see who's on Sunday duty, ask someone to be home, or set up "
        "the Sunday roster.\n\n"
        "Everything is buttons. You never have to type anything."
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
            "The four buttons at the bottom of your screen are how you use me — "
            f"tap {L_HELP} any time for a reminder of what they do.")


def whoami(name: str | None, user_id: int) -> str:
    who = f"You're registered as <b>{esc(name)}</b>.\n" if name else "You're not registered yet.\n"
    return f"{who}Your Telegram ID is <code>{user_id}</code>."


# --- Feature A: dinner ----------------------------------------------------

def dinner_start_prompt(days: int) -> tuple[str, M]:
    return (f"🍜 Start a dinner vote for the next {days} days?",
            M([[B("✅ Start", callback_data=cb("d", "open")),
                B("✖ Never mind", callback_data=cb("x", "close"))]]))


def dinner_poll(poll_id: int, days: list[dict], voted: list[str], waiting: list[str],
                deadline, missing: list[str]) -> tuple[str, M]:
    """The live vote board.

    `days` items: {date, count, required, names, viable}.
    Buttons show the aggregate tally rather than a personal tick — an inline
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
        lines.append("⏳ Waiting on: " + plain_list([esc(n) for n in waiting]))
    if missing:
        lines.append("⚠️ Not registered yet, so we can't reach 5/5: "
                     + plain_list([esc(n) for n in missing]))
    lines += ["", f"<i>Voting closes {t.fmt_datetime(deadline)}.</i>"]

    rows = [[B(f"{'⭐ ' if d['viable'] else ''}{t.fmt_date(d['date'])}  ·  "
               f"{d['count']}/{d['required']}",
               callback_data=cb("d", "v", poll_id, d["date"]))] for d in days]
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


def dinner_locked(event_id: int, day: str, by: str) -> tuple[str, M]:
    return (f"🍜 <b>DINNER IS ON — {t.fmt_date(day)}</b>\n\n"
            f"All five of us are free. {esc(by)} locked it in.\n"
            "I'll remind everyone closer to the day.",
            M([[B("❌ I can't make it any more", callback_data=cb("d", "out", event_id))]]))


def dinner_view_locked(event_id: int, day: str, dropouts: list[str],
                       admin: bool) -> tuple[str, M]:
    lines = [f"🍜 <b>Family dinner — {t.fmt_date(day)}</b>", "", "It's locked in."]
    if dropouts:
        lines += ["", "😔 Can't make it: " + plain_list([esc(n) for n in dropouts])]
    rows = [[B("❌ I can't make it any more", callback_data=cb("d", "out", event_id))]]
    if admin:
        rows.append([B("✖ Cancel dinner", callback_data=cb("d", "ecancel", event_id))])
    return "\n".join(lines), M(rows)


def dinner_nag(mentions: str) -> str:
    return f"🍜 Still waiting on {mentions} to vote for dinner!"


def dinner_deadline_viable(poll_id: int, days: list[dict]) -> tuple[str, M]:
    lines = ["🍜 <b>Time's up on the dinner vote.</b>", "",
             "These days work for everyone — tap one to lock it in:"]
    rows = [[B(f"🔒 Lock in {t.fmt_date(d['date'])}",
               callback_data=cb("d", "lock", poll_id, d["date"]))]
            for d in days if d["viable"]]
    return "\n".join(lines), M(rows)


def dinner_deadline_none(poll_id: int, days: int) -> tuple[str, M]:
    return ("😔 <b>No day worked for all five of us.</b>\n\nTry the next stretch of days?",
            M([[B(f"🔁 Try the next {days} days", callback_data=cb("d", "ext", poll_id))],
               [B("✖ Drop it for now", callback_data=cb("d", "drop", poll_id))]]))


def dinner_reminder(event_id: int, day: str, when: str) -> tuple[str, M]:
    head = {"t3": f"🍜 Family dinner is in 3 days — <b>{t.fmt_date(day)}</b>.",
            "t1": f"🍜 Family dinner is <b>tomorrow</b> ({t.fmt_date(day)}).",
            "day": f"🍜 <b>Family dinner is tonight!</b> ({t.fmt_date(day)})"}[when]
    return (f"{head}\nStill good?",
            M([[B("✅ Still on", callback_data=cb("d", "in", event_id)),
                B("❌ Can't make it", callback_data=cb("d", "out", event_id))]]))


def dinner_dropout(event_id: int, who: str, day: str) -> tuple[str, M]:
    return (f"😔 {who} can't make dinner on {t.fmt_date(day)} any more.\n\nWhat now?",
            M([[B("🔁 Find another day", callback_data=cb("d", "revote", event_id))],
               [B("✖ Cancel this dinner", callback_data=cb("d", "ecancel", event_id))]]))


def dinner_back_in(who: str, day: str) -> str:
    return f"👍 {who} is back on for {t.fmt_date(day)}."


def dinner_cancelled(day: str) -> str:
    return f"✖ Dinner on {t.fmt_date(day)} is cancelled."


def dinner_poll_cancelled() -> str:
    return "✖ Dinner vote cancelled."


def dinner_dropped() -> str:
    return "OK — no dinner vote for now. Tap 🍜 Dinner whenever you want to try again."


def dinner_done(day: str) -> str:
    return f"🥢 Hope dinner was good! ({t.fmt_date(day)})"


def dinner_already_locked(day: str) -> str:
    return f"Dinner is already locked in for {t.fmt_date(day)} 🍜"


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

def roster_menu(has_roster: bool) -> tuple[str, M]:
    rows = [[B("👀 View roster", callback_data=cb("r", "view"))],
            [B("🏠 Ask someone to be home", callback_data=cb("h", "new", "cov"))]]
    label = "⚙️ Set up the Sunday roster" if not has_roster else "⚙️ Re-run Sunday availability"
    rows.append([B(label, callback_data=cb("r", "setup"))])
    return "📅 <b>Roster</b>\nWhat do you need?", M(rows)


def roster_round(round_id: int, days: list[dict], responded: list[str],
                 waiting: list[str], deadline) -> tuple[str, M]:
    lines = ["📅 <b>SUNDAY DUTY — who's free?</b>",
             "Evenings after 6pm. Tap every Sunday you <b>can</b> do.", ""]
    for day in days:
        who = ", ".join(esc(n) for n in day["names"]) or "nobody yet"
        lines.append(f"<b>{t.fmt_date(day['date'])}</b> — {who}")
    lines.append("")
    if responded:
        lines.append("✅ Answered: " + plain_list([esc(n) for n in responded]))
    if waiting:
        lines.append("⏳ Waiting on: " + plain_list([esc(n) for n in waiting]))
    lines += ["", f"<i>Closes {t.fmt_datetime(deadline)}, then I'll share it out fairly.</i>"]

    rows = [[B(f"{t.fmt_date(d['date'])}  ·  {d['count']} free",
               callback_data=cb("r", "av", round_id, d["date"]))] for d in days]
    rows.append([B("🙅 None of these work for me",
                   callback_data=cb("r", "none", round_id))])
    return "\n".join(lines), M(rows)


def roster_result(slots: list[dict], unfilled: list[str]) -> tuple[str, M]:
    lines = ["📅 <b>SUNDAY ROSTER</b>", ""]
    for s in slots:
        who = esc(s["name"]) if s["name"] else "⚠️ nobody yet"
        lines.append(f"<b>{t.fmt_date(s['date'])}</b> — {who}")
    rows = [[B(f"🙋 I'll take {t.fmt_date(dd)}", callback_data=cb("r", "take", dd))]
            for dd in unfilled]
    if unfilled:
        lines += ["", "⚠️ Still needs someone: "
                  + plain_list([t.fmt_date(dd) for dd in unfilled])]
    return "\n".join(lines), M(rows)


def roster_view(slots: list[dict], helps: list[dict], viewer_id: int | None) -> tuple[str, M]:
    lines = ["📅 <b>What's coming up</b>", ""]
    rows = []
    if slots:
        lines.append("<b>Sunday duty</b>")
        for s in slots:
            who = esc(s["name"]) if s["name"] else "⚠️ nobody yet"
            lines.append(f"  {t.fmt_date(s['date'])} — {who}")
            if s["assignee"] is not None and s["assignee"] == viewer_id:
                rows.append([B(f"🔁 I can't do {t.fmt_date(s['date'])}",
                               callback_data=cb("r", "swap", s["date"]))])
            if s["assignee"] is None:
                rows.append([B(f"🙋 I'll take {t.fmt_date(s['date'])}",
                               callback_data=cb("r", "take", s["date"]))])
    else:
        lines.append("No Sunday roster set up yet.")
    if helps:
        lines += ["", "<b>Help requests</b>"]
        for h in helps:
            icon = "🏠" if h["kind"] == "coverage" else "🐕"
            who = esc(h["claimant"]) if h["claimant"] else "⚠️ nobody yet"
            lines.append(f"  {icon} {t.fmt_date(h['req_date'])} "
                         f"{t.window_label(h['time_window'])} — {who}")
    return "\n".join(lines), M(rows)


def roster_no_kids() -> str:
    return ("I need the kids registered before I can build a roster — "
            "tap your names on the setup message first.")


def roster_nag(mentions: str) -> str:
    return f"📅 Still need Sunday availability from {mentions}!"


def roster_unfilled_alert(dates: list[str]) -> tuple[str, M]:
    lines = ["⚠️ <b>No cover for:</b>", ""]
    lines += [f"  {t.fmt_date(dd)}" for dd in dates]
    lines += ["", "Anyone able to step in?"]
    rows = [[B(f"🙋 I'll take {t.fmt_date(dd)}", callback_data=cb("r", "take", dd))]
            for dd in dates]
    return "\n".join(lines), M(rows)


def roster_taken(who: str, day: str) -> str:
    return f"🙌 {who} has {t.fmt_date(day)} covered."


def roster_swap_open(who: str, day: str) -> tuple[str, M]:
    return (f"🔁 {who} can't do Sunday duty on {t.fmt_date(day)}.\n\nAnyone able to swap in?",
            M([[B(f"🙋 I'll take {t.fmt_date(day)}", callback_data=cb("r", "take", day))]]))


def roster_reminder(day: str, mention_text: str, tomorrow: bool) -> str:
    when = "tomorrow evening" if tomorrow else "this evening"
    return f"🔔 {mention_text} — you're on home duty {when} ({t.fmt_date(day)}), after 6pm."


def roster_not_yours() -> str:
    return "That's not your Sunday 🙂"


def roster_already_assigned(who: str) -> str:
    return f"{who} already has that one."


def roster_round_open() -> str:
    return "There's already a Sunday availability round running — scroll up to it 📅"


def kids_only() -> str:
    return "This one's for the three of you kids 🙂"
