"""Сквозной сценарий: хендлеры вызываются напрямую, Telegram замокан."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message

from bot import delivery, handlers, ui
from bot.config import Config
from bot.content import load_content
from bot.db import Database, now_iso
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
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


def make_cb():
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=USER_ID, username="u")
    cb.message = message
    cb.answer = AsyncMock()
    return cb


def last_text(cb) -> str:
    return cb.message.edit_text.call_args.args[0]


def sent_texts(bot) -> list[str]:
    return [c.args[1] for c in bot.send_message.call_args_list]


def assert_full_result(bot, cb):
    """Результат: гейт/вопрос удалён, картинка, расшифровка, финальное сообщение."""
    cb.message.delete.assert_awaited()
    bot.send_photo.assert_awaited_once()
    texts = sent_texts(bot)
    assert "Твой результат" in texts[-2]
    assert "В сексе" in texts[-2] and "Точка роста" in texts[-2]
    assert "Важно помнить" in texts[-2]
    assert "близость" in texts[-1]


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
    assert_full_result(bot, cb)
    assert (await db.get_session(sid)).unlocked_at

    stats = await db.stats()
    assert stats["unlocked_via_gate"] == 1


async def test_already_subscribed_skips_gate(db, config):
    bot = make_bot(ChatMemberStatus.MEMBER)
    cb, sid = await run_quiz(db, config, bot)
    assert_full_result(bot, cb)
    s = await db.get_session(sid)
    assert s.unlocked_at and not s.gate_shown_at


async def test_restricted_member_counts(db, config):
    bot = make_bot(ChatMemberStatus.RESTRICTED)
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.RESTRICTED, is_member=True
    )
    cb, _ = await run_quiz(db, config, bot)
    assert_full_result(bot, cb)


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


async def test_start_quiz_sends_scale_then_first_question(db):
    cb = make_cb()
    await handlers.on_start_quiz(cb, db, CONTENT)
    first, second = (c.args[0] for c in cb.message.answer.call_args_list)
    assert "по шкале от <b>1 до 5</b>" in first
    assert "Вопрос 1 из 10" in second


async def test_final_cta_button_goes_to_contact(db, config):
    bot = make_bot(ChatMemberStatus.MEMBER)
    cfg = replace(config, contact="dasha_sexolog")
    await run_quiz(db, cfg, bot)
    kb = bot.send_message.call_args_list[-1].kwargs["reply_markup"]
    url = kb.inline_keyboard[0][0].url
    assert url.startswith("https://t.me/dasha_sexolog?text=")


async def test_diary_sent_only_if_quiz_not_started(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    await db.upsert_user(USER_ID, "u", None)
    since = now_iso()
    await delivery._diary_later(bot, db, CONTENT, USER_ID, 0, since)
    bot.send_document.assert_awaited_once()
    # Второй раз не шлём.
    await delivery._diary_later(bot, db, CONTENT, USER_ID, 0, since)
    bot.send_document.assert_awaited_once()


async def test_diary_skipped_when_quiz_started(db, config):
    bot = make_bot(ChatMemberStatus.LEFT)
    since = now_iso()
    await handlers.on_start_quiz(make_cb(), db, CONTENT)
    await delivery._diary_later(bot, db, CONTENT, USER_ID, 0, since)
    bot.send_document.assert_not_awaited()
