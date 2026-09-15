"""Bot entry point: wiring, handlers, and the tick loop.

Long polling, so there is no inbound port, no TLS and no reverse-proxy
change on the droplet (spec 9.1).
"""
from __future__ import annotations

import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from app import boards, config, db, dinner, helpreq, roster, scheduler, tg

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO))
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fambot")


# --- guards ----------------------------------------------------------------

def in_group(update: Update) -> bool:
    chat_id = db.group_chat_id()
    return chat_id is not None and update.effective_chat.id == chat_id


async def _reply(update: Update, text: str, markup=None) -> None:
    await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


# --- commands --------------------------------------------------------------

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bind the bot to this group and start registration.

    Open to anyone the first time (nobody is an admin before registration);
    admin-only afterwards.
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await _reply(update, "Run /setup inside the family group, not here.")
        return
    existing = db.group_chat_id()
    if existing is not None and existing != chat.id:
        await _reply(update, "Fambot is already set up in another group.")
        return
    if existing is not None and not db.is_admin(update.effective_user.id):
        if db.registered():
            await _reply(update, "Only an admin can re-run setup.")
            return

    db.set_setting("group_chat_id", chat.id)
    await update.effective_message.reply_text(
        boards.setup_done(), reply_markup=boards.reply_keyboard(), parse_mode="HTML")
    text, markup = boards.registration(db.members())
    msg = await context.bot.send_message(chat.id, text, reply_markup=markup,
                                         parse_mode="HTML")
    db.set_setting("registration_message_id", msg.message_id)
    await context.bot.send_message(chat.id, boards.help_text(), parse_mode="HTML")
    roster.schedule_topup()


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    m = db.member_by_user(user.id)
    await _reply(update, boards.whoami(m["name"] if m else None, user.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, boards.help_text())


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type in ("group", "supergroup"):
        await update.effective_message.reply_text(
            boards.help_text(), reply_markup=boards.reply_keyboard(), parse_mode="HTML")
    else:
        await _reply(update, boards.help_text())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin escape hatch: close whatever is currently open."""
    if not db.is_admin(update.effective_user.id):
        await _reply(update, "That one's admin-only.")
        return
    closed = []
    poll = dinner.open_poll()
    if poll is not None:
        dinner.close_poll(poll["id"], "cancelled")
        closed.append("the dinner vote")
    rnd = roster.open_round()
    if rnd is not None:
        db.x("UPDATE roster_rounds SET status = 'cancelled' WHERE id = ?", (rnd["id"],))
        db.cancel_jobs("roster_nag", rnd["id"])
        db.cancel_jobs("roster_deadline", rnd["id"])
        closed.append("the Sunday availability round")
    for req in helpreq.open_requests():
        helpreq.cancel(req["id"], update.effective_user.id)
        closed.append(f"help request #{req['id']}")
    await _reply(update, ("✖ Closed: " + tg.plain_list(closed)) if closed
                 else "Nothing open to cancel.")


async def cmd_dinner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_group(update):
        await dinner.entry(context.bot, update.effective_user.id)


async def cmd_rush(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_group(update):
        await helpreq.entry_dog(context.bot)


async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_group(update):
        await roster.entry(context.bot)


async def _require_group(update: Update) -> bool:
    if db.group_chat_id() is None:
        await _reply(update, boards.not_set_up())
        return False
    if not in_group(update):
        await _reply(update, boards.wrong_chat())
        return False
    return True


# --- the four keyboard labels ---------------------------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ONLY text this bot acts on.

    Anything that is not one of the four fixed labels returns immediately -
    it is never parsed, stored or logged. That keeps the bot deaf to normal
    family chatter even when Telegram's privacy mode is off (see README).
    """
    msg = update.effective_message
    text = (msg.text or "").strip()
    if text not in boards.LABELS:
        return
    if not await _require_group(update):
        return

    bot = context.bot
    if text == boards.L_DINNER:
        await dinner.entry(bot, update.effective_user.id)
    elif text == boards.L_RUSH:
        await helpreq.entry_dog(bot)
    elif text == boards.L_ROSTER:
        await roster.entry(bot)
    elif text == boards.L_HELP:
        await tg.send_group(bot, boards.help_text())


# --- callbacks -------------------------------------------------------------

async def on_setup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = tg.parse_cb(update.callback_query.data)
    if parts[1] != "reg":
        return
    slug, user = parts[2], update.effective_user
    member = db.member_by_slug(slug)
    if member is None:
        await tg.toast(update, "I don't have that name on file.", alert=True)
        return
    db.register_member(slug, user.id, user.username)
    await tg.toast(update, f"Got it — you're {member['name']} 👋")
    text, markup = boards.registration(db.members())
    await tg.edit_or_repost(context.bot, update.effective_chat.id,
                            update.callback_query.message.message_id, text, markup)


async def on_misc_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = tg.parse_cb(update.callback_query.data)
    if parts[1] == "help":
        await tg.toast(update, "")
        await tg.send_group(context.bot, boards.help_text())
    else:
        await tg.toast(update, "")
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


# --- lifecycle -------------------------------------------------------------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("dinner", "Start or see the family dinner vote"),
        BotCommand("rush", "Ask for help with Rush"),
        BotCommand("roster", "See the Sunday roster"),
        BotCommand("help", "What the buttons do"),
        BotCommand("whoami", "Show my Telegram ID"),
        BotCommand("setup", "Set Fambot up in this group (admin)"),
        BotCommand("cancel", "Close anything open (admin)"),
    ])
    # Idempotent: keeps the daily top-up check armed across restarts.
    roster.schedule_topup()
    me = await app.bot.get_me()
    log.info("Fambot up as @%s | group=%s | %d member(s) registered",
             me.username, db.group_chat_id(), len(db.registered()))


def build() -> Application:
    db.init()
    app = (Application.builder()
           .token(config.BOT_TOKEN)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler(["help", "start"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("dinner", cmd_dinner))
    app.add_handler(CommandHandler("rush", cmd_rush))
    app.add_handler(CommandHandler("roster", cmd_roster))

    app.add_handler(CallbackQueryHandler(dinner.on_callback, pattern=r"^d\|"))
    app.add_handler(CallbackQueryHandler(helpreq.on_callback, pattern=r"^h\|"))
    app.add_handler(CallbackQueryHandler(roster.on_callback, pattern=r"^r\|"))
    app.add_handler(CallbackQueryHandler(on_setup_cb, pattern=r"^s\|"))
    app.add_handler(CallbackQueryHandler(on_misc_cb, pattern=r"^x\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # The durable jobs table is the source of truth; this just scans it.
    app.job_queue.run_repeating(scheduler.tick, interval=config.TICK_SECONDS, first=5)
    return app


def main() -> int:
    if config.missing_token():
        log.error("BOT_TOKEN is not set. Copy .env.example to .env and add the "
                  "token from @BotFather.")
        return 1
    app = build()
    app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"],
                    drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
