"""Offline test harness: a fake Telegram Bot plus a fresh database per test.

Nothing here touches the network. The fake bot records what would have been
sent so tests can assert on the family's actual experience.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, tg  # noqa: E402

MEMBERS = [
    {"slug": "dad", "name": "Dad", "parent": True, "driver": True},
    {"slug": "mom", "name": "Mom", "parent": True},
    {"slug": "mark", "name": "Mark", "kid": True, "driver": True, "admin": True},
    {"slug": "dawn", "name": "Dawn", "kid": True},
    {"slug": "luke", "name": "Luke", "kid": True},
]
IDS = {"dad": 101, "mom": 102, "mark": 103, "dawn": 104, "luke": 105}
GROUP = -1001234567890


@dataclass
class Sent:
    chat_id: int
    text: str
    markup: object = None
    message_id: int = 0


class FakeBot:
    """Stands in for telegram.Bot. Records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.edits: list[Sent] = []
        self.pinned: list[int] = []
        self.unpinned: list[int] = []
        self._next_id = 1000
        self.fail_edit = False

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._next_id += 1
        self.sent.append(Sent(chat_id, text, reply_markup, self._next_id))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, **kw):
        if self.fail_edit:
            from telegram.error import BadRequest
            raise BadRequest("message to edit not found")
        self.edits.append(Sent(chat_id, text, reply_markup, message_id))
        return SimpleNamespace(message_id=message_id)

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.pinned.append(message_id)

    async def unpin_chat_message(self, chat_id, message_id, **kw):
        self.unpinned.append(message_id)

    # --- helpers for assertions -------------------------------------------
    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent]

    @property
    def last(self) -> str:
        return self.sent[-1].text if self.sent else ""

    @property
    def board(self) -> str:
        """Most recent rendering of a live board, edit or fresh post."""
        return (self.edits[-1].text if self.edits else self.last)

    def said(self, needle: str) -> bool:
        return any(needle in s.text for s in self.sent + self.edits)

    def buttons(self) -> list[str]:
        src = self.edits[-1] if self.edits else (self.sent[-1] if self.sent else None)
        if src is None or src.markup is None:
            return []
        return [b.text for row in src.markup.inline_keyboard for b in row]

    def callbacks(self) -> list[str]:
        src = self.edits[-1] if self.edits else (self.sent[-1] if self.sent else None)
        if src is None or src.markup is None:
            return []
        return [b.callback_data for row in src.markup.inline_keyboard for b in row]

    def reset(self) -> None:
        self.sent.clear()
        self.edits.clear()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A new SQLite file per test, seeded with the five family members."""
    db.close()
    members_file = tmp_path / "members.json"
    members_file.write_text(json.dumps({"members": MEMBERS}))
    db.init(str(tmp_path / "fambot.db"), str(members_file))
    db.set_setting("group_chat_id", GROUP)
    for slug, uid in IDS.items():
        db.register_member(slug, uid)
    # No debounce in tests - render immediately so assertions see the board.
    tg.debouncer.delay = 0
    # Close roster rounds as soon as the pending job is run, not 2 min later.
    db.set_setting("roster_close_grace_seconds", 0)
    yield
    db.close()


@pytest.fixture
def unregister_all():
    def _do(*slugs):
        for slug in slugs:
            db.x("UPDATE members SET user_id = NULL WHERE slug = ?", (slug,))
    return _do


def fake_update(data: str, user_id: int, message_id: int = 500):
    """A minimal stand-in for a callback-button tap."""
    answers: list[dict] = []

    async def answer(text=None, show_alert=False):
        answers.append({"text": text, "alert": show_alert})

    qry = SimpleNamespace(data=data, answer=answer,
                          message=SimpleNamespace(message_id=message_id))
    upd = SimpleNamespace(callback_query=qry,
                          effective_user=SimpleNamespace(id=user_id, username=None),
                          effective_chat=SimpleNamespace(id=GROUP))
    upd.answers = answers
    return upd


def ctx(bot):
    return SimpleNamespace(bot=bot)


async def settle(bot):
    """Run whatever the last interaction queued (the roster close timer, etc.).

    Handlers are invoked directly rather than through scheduler.run_due so the
    result never depends on what time of day the suite happens to run.
    """
    from app import scheduler
    for job in db.due_jobs():
        handler = scheduler.HANDLERS.get(job["kind"])
        db.finish_job(job["id"])
        if handler is not None:
            await handler(bot, job)
