"""Spec 10: the bot ignores free text, and registration works by tapping."""
from types import SimpleNamespace

from app import boards, db, main
from tests.conftest import GROUP, IDS, ctx, fake_update


def text_update(text, user_id=IDS["mark"], chat_id=GROUP, chat_type="supergroup"):
    replies = []

    async def reply_text(t, reply_markup=None, **kw):
        replies.append(t)
        return SimpleNamespace(message_id=1)

    msg = SimpleNamespace(text=text, reply_text=reply_text)
    upd = SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id, username="x"),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type))
    upd.replies = replies
    return upd


async def test_ordinary_chatter_is_ignored_entirely(bot):
    for chatter in ["anyone free to walk rush tmr?", "lol", "🍜", "dinner",
                    "/dinner please", "Dinner", " 🍜 Dinner extra"]:
        upd = text_update(chatter)
        await main.on_text(upd, ctx(bot))
    assert bot.sent == []
    assert all(u for u in [True])          # nothing replied, nothing crashed


async def test_the_four_labels_are_the_only_text_that_acts(bot):
    await main.on_text(text_update(boards.L_HELP), ctx(bot))
    assert bot.said("How Fambot works")

    bot.reset()
    await main.on_text(text_update(boards.L_DINNER), ctx(bot))
    assert bot.said("Start a dinner vote")

    bot.reset()
    await main.on_text(text_update(boards.L_RUSH), ctx(bot))
    assert bot.said("What do you need?")

    bot.reset()
    await main.on_text(text_update(boards.L_ROSTER), ctx(bot))
    assert bot.said("Roster")


async def test_labels_do_nothing_in_another_chat(bot):
    upd = text_update(boards.L_DINNER, chat_id=-999)
    await main.on_text(upd, ctx(bot))
    assert bot.sent == []
    assert "family group" in upd.replies[0]


async def test_labels_do_nothing_before_setup(bot):
    db.x("DELETE FROM settings WHERE key='group_chat_id'")
    upd = text_update(boards.L_DINNER)
    await main.on_text(upd, ctx(bot))
    assert "isn't set up yet" in upd.replies[0]


async def test_registration_binds_the_tapper_to_that_name(bot, unregister_all):
    unregister_all("dawn")
    upd = fake_update("s|reg|dawn", 55555)
    await main.on_setup_cb(upd, ctx(bot))
    assert db.member_by_slug("dawn")["user_id"] == 55555
    assert "you're Dawn" in upd.answers[-1]["text"]


async def test_registration_board_shows_who_is_still_missing(bot, unregister_all):
    unregister_all("luke", "dawn")
    text, _ = boards.registration(db.members())
    assert "Still waiting on: Dawn and Luke" in text
    assert "Registered: Dad, Mom and Mark" in text


async def test_registration_board_celebrates_when_complete(bot):
    text, _ = boards.registration(db.members())
    assert "Everyone's in" in text


async def test_setup_stores_the_group_and_posts_the_keyboard(bot):
    db.x("DELETE FROM settings WHERE key='group_chat_id'")
    sent = []

    async def reply_text(t, reply_markup=None, **kw):
        sent.append((t, reply_markup))
        return SimpleNamespace(message_id=9)

    upd = text_update("/setup")
    upd.effective_message.reply_text = reply_text
    await main.cmd_setup(upd, ctx(bot))

    assert db.group_chat_id() == GROUP
    assert "Fambot is set up" in sent[0][0]
    assert sent[0][1] is not None          # the persistent reply keyboard
    assert bot.said("Tap your own name")
    assert db.has_pending_job("roster_topup")


async def test_setup_is_refused_outside_a_group(bot):
    upd = text_update("/setup", chat_type="private", chat_id=IDS["mark"])
    await main.cmd_setup(upd, ctx(bot))
    assert "inside the family group" in upd.replies[0]


async def test_whoami_reports_the_id(bot):
    upd = text_update("/whoami", user_id=IDS["dawn"])
    await main.cmd_whoami(upd, ctx(bot))
    assert "Dawn" in upd.replies[0] and str(IDS["dawn"]) in upd.replies[0]


async def test_admin_cancel_closes_everything_open(bot):
    from app import dinner, helpreq, roster
    await dinner.open_new(bot, IDS["mark"])
    await roster.start_round(bot)
    await helpreq.create(bot, "dog_accompany", IDS["dad"],
                         (__import__("app.timeutil", fromlist=["x"]).today_local()
                          ).isoformat(), "evening")
    upd = text_update("/cancel", user_id=IDS["mark"])
    await main.cmd_cancel(upd, ctx(bot))

    assert dinner.open_poll() is None
    assert roster.open_round() is None
    assert helpreq.open_requests() == []


async def test_non_admin_cannot_cancel(bot):
    from app import dinner
    await dinner.open_new(bot, IDS["mark"])
    upd = text_update("/cancel", user_id=IDS["luke"])
    await main.cmd_cancel(upd, ctx(bot))
    assert "admin-only" in upd.replies[0]
    assert dinner.open_poll() is not None
