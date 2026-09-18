"""Spec 10: the bot only hears its own commands, and registration is a tap."""
from types import SimpleNamespace

from telegram import ReplyKeyboardRemove

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


async def test_nothing_listens_to_free_text(bot):
    """Ordinary chatter can't reach the bot - no MessageHandler exists."""
    assert not hasattr(main, "on_text")
    src = open(main.__file__, encoding="utf-8").read()
    assert "app.add_handler(MessageHandler" not in src


async def test_each_command_opens_its_flow(bot):
    await main.cmd_help(text_update("/help"), ctx(bot))

    bot.reset()
    await main.cmd_dinner(text_update("/dinner"), ctx(bot))
    assert bot.said("Start a dinner vote")

    bot.reset()
    await main.cmd_rush(text_update("/rush"), ctx(bot))
    assert bot.said("What do you need?")

    bot.reset()
    await main.cmd_roster(text_update("/roster"), ctx(bot))
    assert bot.said("Roster")


async def test_commands_do_nothing_in_another_chat(bot):
    upd = text_update("/dinner", chat_id=-999)
    await main.cmd_dinner(upd, ctx(bot))
    assert bot.sent == []
    assert "family group" in upd.replies[0]


async def test_commands_do_nothing_before_setup(bot):
    db.x("DELETE FROM settings WHERE key='group_chat_id'")
    upd = text_update("/dinner")
    await main.cmd_dinner(upd, ctx(bot))
    assert "isn't set up yet" in upd.replies[0]


async def test_help_lists_the_commands(bot):
    text = boards.help_text()
    for cmd in (boards.C_DINNER, boards.C_RUSH, boards.C_ROSTER, boards.C_HELP):
        assert cmd in text


async def test_start_in_the_group_takes_the_old_keyboard_away(bot):
    sent = []

    async def reply_text(t, reply_markup=None, **kw):
        sent.append((t, reply_markup))
        return SimpleNamespace(message_id=9)

    upd = text_update("/start")
    upd.effective_message.reply_text = reply_text
    await main.cmd_start(upd, ctx(bot))
    assert isinstance(sent[0][1], ReplyKeyboardRemove)


async def test_the_switch_notice_is_posted_once(bot):
    app = SimpleNamespace(bot=bot)
    await main._retire_reply_keyboard(app)
    assert bot.said("Fambot changed slightly")
    assert isinstance(bot.sent[-1].markup, ReplyKeyboardRemove)

    bot.reset()
    await main._retire_reply_keyboard(app)
    assert bot.sent == []               # a restart never reposts it


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


async def test_setup_stores_the_group_and_clears_any_old_keyboard(bot):
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
    assert isinstance(sent[0][1], ReplyKeyboardRemove)
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
