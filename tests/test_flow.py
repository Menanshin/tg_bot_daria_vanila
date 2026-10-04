"""Сквозной сценарий: хендлеры вызываются напрямую, Telegram замокан."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message

from bot import handlers, ui
from bot.config import Config
from bot.content import load_content
from bot.db import Database
from bot.reminders import send_due_reminders

CONTENT = load_content()
USER_ID = 42


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def config():
    return Config(
        bot_token="x", channel="@vanillapropsy", admin_ids=frozenset({1}),
        db_path=":memory:", reminder_hours=0, log_level="INFO",
    )


def make_bot(status: ChatMemberStatus):
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))
    bot.me = AsyncMock(return_value=SimpleNamespace(id=7, username="quiz_bot"))
    bot.send_message = AsyncMock()
    return bot


def make_cb():
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=USER_ID, username="u")
    cb.message = message
    cb.answer = AsyncMock()
    return cb


def last_text(cb) -> str:
    return cb.message.edit_text.call_args.args[0]


async def run_quiz(db, config, bot, value=5):
    cb = make_cb()
    await handlers.on_start_quiz(cb, db, CONTENT)
    session = await db.latest_session(USER_ID)
    for i in range(len(CONTENT.questions)):
        await handlers.on_answer(
            cb, ui.AnswerCb(session=session.id, q=i, v=value), db, CONTENT, config, bot
        )
    return cb, session.id


async def test_gate_then_unlock(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    cb, sid = await run_quiz(db, config, bot)
    assert "🔒" in last_text(cb)
    s = await db.get_session(sid)
    assert s.finished_at and s.gate_shown_at and not s.unlocked_at

    # Не подписан — алерт, результат закрыт.
    await handlers.on_check(cb, db, CONTENT, config, bot)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    assert not (await db.get_session(sid)).unlocked_at

    # Подписался.
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
    await handlers.on_check(cb, db, CONTENT, config, bot)
    assert "Твой результат" in last_text(cb)
    assert (await db.get_session(sid)).unlocked_at

    stats = await db.stats()
    assert stats["unlocked_via_gate"] == 1


async def test_already_subscribed_skips_gate(db, config):
    bot = make_bot(ChatMemberStatus.MEMBER)
    cb, sid = await run_quiz(db, config, bot)
    assert "Твой результат" in last_text(cb)
    s = await db.get_session(sid)
    assert s.unlocked_at and not s.gate_shown_at


async def test_restricted_member_counts(db, config):
    bot = make_bot(ChatMemberStatus.RESTRICTED)
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.RESTRICTED, is_member=True
    )
    cb, _ = await run_quiz(db, config, bot)
    assert "Твой результат" in last_text(cb)


async def test_double_tap_and_stale_buttons_ignored(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    cb = make_cb()
    await handlers.on_start_quiz(cb, db, CONTENT)
    sid = (await db.latest_session(USER_ID)).id
    await handlers.on_answer(cb, ui.AnswerCb(session=sid, q=0, v=4), db, CONTENT, config, bot)
    await handlers.on_answer(cb, ui.AnswerCb(session=sid, q=0, v=1), db, CONTENT, config, bot)
    assert (await db.get_session(sid)).answers == [4]


async def test_back(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    cb = make_cb()
    await handlers.on_start_quiz(cb, db, CONTENT)
    sid = (await db.latest_session(USER_ID)).id
    for i, v in enumerate([2, 3]):
        await handlers.on_answer(cb, ui.AnswerCb(session=sid, q=i, v=v), db, CONTENT, config, bot)
    await handlers.on_back(cb, ui.BackCb(session=sid, q=2), db, CONTENT)
    assert (await db.get_session(sid)).answers == [2]
    assert "Вопрос 2 из 10" in last_text(cb)


async def test_foreign_session_rejected(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    await db.upsert_user(999, None, None)
    other = await db.new_session(999)
    cb = make_cb()
    await handlers.on_answer(cb, ui.AnswerCb(session=other.id, q=0, v=3), db, CONTENT, config, bot)
    assert (await db.get_session(other.id)).answers == []


async def test_reminder_sent_once(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    await run_quiz(db, config, bot)
    # reminder_hours=0 в фикстуре — напоминание «созрело» сразу.
    assert await send_due_reminders(bot, db, CONTENT, config) == 1
    assert await send_due_reminders(bot, db, CONTENT, config) == 0
    assert bot.send_message.await_count == 1


async def test_reminder_skips_already_subscribed(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    await run_quiz(db, config, bot)
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
    assert await send_due_reminders(bot, db, CONTENT, config) == 0
    bot.send_message.assert_not_awaited()


def test_parse_source():
    assert handlers.parse_source("insta_reels-1") == "insta_reels-1"
    assert handlers.parse_source("bad source!") is None
    assert handlers.parse_source(None) is None
