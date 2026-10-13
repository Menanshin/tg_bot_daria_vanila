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


async def test_booking_through_dispatcher():
    """Запись: /book → тип → день → время → подтверждение; Даша задаёт часы через /schedule."""
    from bot import booking as bk
    from bot import booking_ui as bui
    from bot.booking_handlers import router as booking_router

    db = Database(":memory:")
    await db.connect()
    # USER (42) — одновременно Даша: так проверяем и её команды, и клиентский путь.
    config = Config(
        bot_token="x", channel="@vanillapropsy", admin_ids=frozenset(),
        db_path=":memory:", reminder_hours=0, log_level="INFO", booking_owner_id=USER.id,
    )
    dp = Dispatcher(db=db, content=CONTENT, config=config)
    dp.include_router(booking_router)
    dp.include_router(router)
    bot = FakeBot()
    try:
        await dp.feed_update(bot, msg_update("/schedule"))
        assert "Расписание" in bot.calls[-1].text
        await dp.feed_update(bot, cb_update(bui.SchedWeekdayCb(wd=2).pack()))
        await dp.feed_update(bot, msg_update("10-14, 16:00-20:00"))
        assert "Ср: 10:00–14:00, 16:00–20:00" in bot.calls[-1].text
        await dp.feed_update(bot, cb_update(bui.SchedWeekdayCb(wd=2).pack()))
        await dp.feed_update(bot, msg_update("ерунда"))
        assert "Не понимаю" in bot.calls[-1].text
        await dp.feed_update(bot, msg_update("/cancel"))
        assert "ничего не меняю" in bot.calls[-1].text

        await dp.feed_update(bot, msg_update("/start"))
        start_msg = next(
            c for c in reversed(bot.calls)
            if isinstance(c, SendMessage) and c.reply_markup is not None
            and not isinstance(c.reply_markup, ReplyKeyboardMarkup)
        )
        book_cb = start_msg.reply_markup.inline_keyboard[1][0].callback_data
        await dp.feed_update(bot, cb_update(book_cb))
        assert "Запись на онлайн-встречу" in [
            c for c in bot.calls if isinstance(c, SendMessage)
        ][-1].text

        await dp.feed_update(bot, msg_update("/book"))
        assert "Запись на онлайн-встречу" in bot.calls[-1].text
        await dp.feed_update(bot, cb_update(bui.BookKindCb(kind=bk.Kind.FULL).pack()))
        days_kb = next(
            c for c in reversed(bot.calls) if isinstance(c, EditMessageText)
        ).reply_markup
        await dp.feed_update(bot, cb_update(days_kb.inline_keyboard[0][0].callback_data))
        times_kb = bot.calls[-2].reply_markup
        first_time = bui.BookTimeCb.unpack(times_kb.inline_keyboard[0][0].callback_data)
        await dp.feed_update(bot, cb_update(
            bui.BookConfirmCb(kind=first_time.kind, at=first_time.at).pack()
        ))
        sent = [c for c in bot.calls if isinstance(c, SendMessage)]
        assert "Новая заявка" in sent[-1].text
        (b,) = await db.user_bookings(USER.id, bk.utcnow())
        assert b.status is bk.Status.PENDING
    finally:
        await db.close()
        booking_router._parent_router = None
        router._parent_router = None
