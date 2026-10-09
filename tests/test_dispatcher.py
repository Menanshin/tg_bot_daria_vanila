"""Апдейты проходят через настоящий Dispatcher: проверяем роутинг и внедрение зависимостей."""

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChatMember,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot import ui
from bot.config import Config
from bot.content import load_content
from bot.db import Database
from bot.handlers import router

CONTENT = load_content()
USER = User(id=42, is_bot=False, first_name="Test", username="tester")
BOT_USER = User(id=7, is_bot=True, first_name="Quiz", username="quiz_bot")
CHAT = Chat(id=42, type="private")


class FakeBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:TEST-TOKEN-NOT-REAL")
        self.calls: list[TelegramMethod] = []

    async def __call__(self, method, request_timeout=None):  # type: ignore[override]
        self.calls.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, GetChatMember):
            return ChatMemberLeft(user=USER)
        if isinstance(method, SendMessage | EditMessageText):
            return Message(
                message_id=1, date=datetime.now(UTC), chat=CHAT, text=method.text
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"unexpected call {type(method).__name__}")


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db.connect()
    config = Config(
        bot_token="x", channel="@vanillapropsy", admin_ids=frozenset({42}),
        db_path=":memory:", reminder_hours=24, log_level="INFO",
    )
    dp = Dispatcher(db=db, content=CONTENT, config=config)
    dp.include_router(router)
    bot = FakeBot()
    yield dp, bot, db
    await db.close()
    # router — модульный синглтон: отцепляем, чтобы следующий тест мог подключить его заново.
    router._parent_router = None


_ids = iter(range(1, 10_000))


def msg_update(text: str) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(UTC), chat=CHAT, from_user=USER, text=text,
    ))


def cb_update(data: str) -> Update:
    message = Message(message_id=1, date=datetime.now(UTC), chat=CHAT, from_user=BOT_USER, text="")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=USER, chat_instance="ci", message=message, data=data,
    ))


async def test_full_flow_through_dispatcher(env):
    dp, bot, db = env

    await dp.feed_update(bot, msg_update("/start insta_bio"))
    assert isinstance(bot.calls[-1], SendMessage)

    await dp.feed_update(bot, cb_update(ui.StartCb().pack()))
    session = await db.latest_session(USER.id)
    assert session is not None

    for i in range(len(CONTENT.questions)):
        await dp.feed_update(bot, cb_update(ui.AnswerCb(session=session.id, q=i, v=4).pack()))

    edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
    assert "🔒" in edits[-1].text

    await dp.feed_update(bot, cb_update(ui.CheckCb().pack()))
    alerts = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
    assert alerts, "not subscribed → alert expected"

    await dp.feed_update(bot, msg_update("/stats"))
    stats_text = bot.calls[-1].text
    assert "insta_bio: 1" in stats_text
    assert "Увидели гейт: 1" in stats_text


async def test_admin_stats_button(env):
    dp, bot, _ = env

    await dp.feed_update(bot, msg_update("/start"))
    admin_msgs = [
        c for c in bot.calls
        if isinstance(c, SendMessage) and isinstance(c.reply_markup, ReplyKeyboardMarkup)
    ]
    assert admin_msgs, "admin should get the stats keyboard on /start"
    assert admin_msgs[-1].reply_markup.keyboard[0][0].text == ui.STATS_BUTTON

    await dp.feed_update(bot, msg_update(ui.STATS_BUTTON))
    assert "Воронка" in bot.calls[-1].text

    await dp.feed_update(bot, cb_update(ui.StatsCb().pack()))
    assert any(isinstance(c, EditMessageText) and "Воронка" in c.text for c in bot.calls)


async def test_non_admin_gets_no_stats(env):
    dp, bot, _ = env
    stranger = User(id=99, is_bot=False, first_name="X")
    chat = Chat(id=99, type="private")

    def msg(text: str) -> Update:
        return Update(update_id=next(_ids), message=Message(
            message_id=next(_ids), date=datetime.now(UTC), chat=chat,
            from_user=stranger, text=text,
        ))

    await dp.feed_update(bot, msg("/start"))
    assert not any(
        isinstance(c, SendMessage) and isinstance(c.reply_markup, ReplyKeyboardMarkup)
        for c in bot.calls
    )
    before = len(bot.calls)
    await dp.feed_update(bot, msg(ui.STATS_BUTTON))
    await dp.feed_update(bot, msg("/stats"))
    assert len(bot.calls) == before
