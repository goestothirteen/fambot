"""Telegram plumbing: mentions, resilient board edits, and edit debouncing.

Nothing user-facing lives here - that is boards.py. This module is about
making Telegram behave.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Awaitable, Callable

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from app import config, db

log = logging.getLogger(__name__)

SEP = "|"


# --- callback_data ---------------------------------------------------------
# Budget is 64 bytes, so codes are terse: "d|v|3|2026-09-19" (16 bytes).
# Every flow is stateless - the whole wizard position rides in the token,
# so there is no per-user draft state to expire or leak between people.

def cb(*parts: Any) -> str:
    token = SEP.join(str(p) for p in parts)
    if len(token.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long ({len(token)}): {token}")
    return token


def parse_cb(data: str) -> list[str]:
    return data.split(SEP)


# --- text ------------------------------------------------------------------

def esc(s: Any) -> str:
    """Escape for HTML parse mode. Family names with '&' would otherwise 400."""
    return html.escape(str(s), quote=False)


def mention(user_id: int | None, name: str) -> str:
    """A tappable ping that works without a @username - parents rarely have one."""
    safe = esc(name)
    if user_id is None:
        return safe
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def mention_member(m) -> str:
    return mention(m["user_id"], m["name"])


def mention_list(rows) -> str:
    names = [mention_member(m) for m in rows]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def plain_list(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# --- sending ---------------------------------------------------------------

async def send(bot: Bot, chat_id: int, text: str,
               markup: InlineKeyboardMarkup | None = None,
               **kwargs) -> int | None:
    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kwargs)
        return msg.message_id
    except Forbidden:
        log.error("Bot is not allowed to post in chat %s - was it removed?", chat_id)
    except TelegramError as e:
        log.error("send_message failed in %s: %s", chat_id, e)
    return None


async def send_group(bot: Bot, text: str,
                     markup: InlineKeyboardMarkup | None = None, **kwargs) -> int | None:
    chat_id = db.group_chat_id()
    if chat_id is None:
        log.warning("No group chat configured; dropping message: %.60s", text)
        return None
    return await send(bot, chat_id, text, markup, **kwargs)


async def edit_or_repost(bot: Bot, chat_id: int, message_id: int | None, text: str,
                         markup: InlineKeyboardMarkup | None = None) -> int | None:
    """Keep one live board per activity (spec 2.5).

    Edits in place when it can. If the message is gone, too old, or otherwise
    un-editable, posts a fresh board and returns the new id so the caller can
    store it.
    """
    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=markup, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
            return message_id
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return message_id          # nothing changed; that is fine
            log.info("Board %s un-editable (%s) - reposting", message_id, e)
        except TelegramError as e:
            log.warning("Board edit failed (%s) - reposting", e)
    return await send(bot, chat_id, text, markup)


async def delete_message(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Remove a board that has been superseded, so only one live board remains.

    Telegram raises when the message is already gone (a user deleted it, or it
    is too old to delete). That is exactly the state we want, so swallow it.
    """
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        log.debug("Could not delete %s (%s) - already gone or too old", message_id, e)


async def pin(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id,
                                   disable_notification=True)
        return True
    except TelegramError as e:
        log.info("Could not pin %s (%s) - bot probably lacks admin rights", message_id, e)
        return False


async def unpin(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        log.debug("Could not unpin %s: %s", message_id, e)


async def toast(update, text: str, alert: bool = False) -> None:
    """Instant private feedback on a tap.

    Inline keyboards are shared by everyone in the group, so a button cannot
    show one person's own selection. The toast is how a tapper learns what
    their tap did.
    """
    qry = getattr(update, "callback_query", None)
    if qry is None:
        return
    try:
        await qry.answer(text=text, show_alert=alert)
    except TelegramError as e:
        log.debug("callback answer failed: %s", e)


# --- debounced board rendering --------------------------------------------

class Debouncer:
    """Coalesce a burst of taps into one board edit.

    Someone ticking five dinner dates in three seconds should cost one edit,
    not five - Telegram rate-limits edits, and a flickering board looks broken.
    """

    def __init__(self, delay: float | None = None) -> None:
        self.delay = config.RENDER_DEBOUNCE_SECONDS if delay is None else delay
        self._tasks: dict[str, asyncio.Task] = {}

    def trigger(self, key: str, render: Callable[[], Awaitable[Any]]) -> None:
        old = self._tasks.get(key)
        if old is not None and not old.done():
            old.cancel()
        self._tasks[key] = asyncio.create_task(self._run(key, render))

    async def _run(self, key: str, render: Callable[[], Awaitable[Any]]) -> None:
        try:
            await asyncio.sleep(self.delay)
            await render()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Board render failed for %s", key)
        finally:
            if self._tasks.get(key) is not None and self._tasks[key].done():
                self._tasks.pop(key, None)

    async def flush(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


debouncer = Debouncer()
