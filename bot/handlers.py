"""Хендлеры: /start, /test, прохождение теста, гейт подписки, результат, статистика для админов."""

from __future__ import annotations

import contextlib
import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import (
    KICKED,
    MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
)
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message, User

from bot import delivery, ui
from bot.config import Config
from bot.content import Content
from bot.db import Database, Session
from bot.scoring import AttachmentType, score
from bot.subscription import is_subscribed

log = logging.getLogger(__name__)
router = Router(name="main")

SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_source(args: str | None) -> str | None:
    if args and SOURCE_RE.fullmatch(args):
        return args
    return None


async def safe_delete(message: Message) -> None:
    """Убрать гейт/последний вопрос перед результатом. Не вышло — не страшно."""
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# --- блокировка бота ---
# Telegram сам присылает my_chat_member, когда человек блокирует бота в личке
# (статус kicked) и когда разблокирует (member). Чаты и каналы сюда не попадают.

@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(KICKED))
async def on_bot_blocked(event: ChatMemberUpdated, db: Database) -> None:
    await db.upsert_user(event.from_user.id, event.from_user.username, None)
    await db.set_blocked(event.from_user.id, True)


@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(MEMBER))
async def on_bot_unblocked(event: ChatMemberUpdated, db: Database) -> None:
    await db.upsert_user(event.from_user.id, event.from_user.username, None)
    await db.set_blocked(event.from_user.id, False)


# --- /start ---

@router.message(CommandStart(deep_link=True, magic=F.args == ui.COURSE_DEEP_LINK))
async def cmd_start_course(
    message: Message, db: Database, content: Content, config: Config, bot: Bot
) -> None:
    """t.me/<бот>?start=course — сразу экран курса (кнопка «Подробнее про курс» в постах)."""
    user = message.from_user
    if user is None:
        return
    await db.upsert_user(user.id, user.username, ui.COURSE_DEEP_LINK)
    await db.log_event(user.id, "start")
    await db.log_event(user.id, "course_opened")
    await delivery.send_course(bot, user.id, content)
    delivery.schedule_diary(bot, db, content, config, user.id)


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    user = message.from_user
    if user is None:
        return
    await db.upsert_user(user.id, user.username, parse_source(command.args))
    await db.log_event(user.id, "start")
    text, kb = ui.start_screen(content, booking=config.booking_enabled, contact=config.contact)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    if is_admin(user, config):
        await message.answer(ADMIN_HINT, reply_markup=ui.admin_keyboard())
    delivery.schedule_diary(bot, db, content, config, user.id)


@router.message(Command("test"))
async def cmd_test(message: Message, db: Database, content: Content) -> None:
    if message.from_user is not None:
        await _begin_quiz(message.from_user, message, db, content)


@router.callback_query(ui.StartCb.filter())
async def on_start_quiz(cb: CallbackQuery, db: Database, content: Content) -> None:
    await _begin_quiz(cb.from_user, cb.message, db, content)
    await cb.answer()


@router.callback_query(ui.RestartCb.filter())
async def on_restart(cb: CallbackQuery, db: Database, content: Content) -> None:
    await _begin_quiz(cb.from_user, cb.message, db, content)
    await cb.answer()


async def _begin_quiz(
    user: User, message: Message | None, db: Database, content: Content
) -> None:
    # Приветствие/результат остаются в чате: ниже — шкала ответов, под ней вопросы
    # (одно сообщение, которое редактируется).
    await db.upsert_user(user.id, user.username, None)
    session = await db.new_session(user.id)
    await db.log_event(user.id, "quiz_started")
    text, kb = ui.question_screen(content, session.id, 0)
    if message is not None:
        await message.answer(ui.quiz_intro_text(content))
        await message.answer(text, reply_markup=kb)


# --- разделы меню: «Кто такая Дарья?», «Отзывы» ---

@router.callback_query(ui.MenuCb.filter())
async def on_menu(
    cb: CallbackQuery, callback_data: ui.MenuCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    section, user = callback_data.section, cb.from_user
    if section not in ui.SECTIONS or cb.message is None:
        await cb.answer()
        return
    await db.upsert_user(user.id, user.username, None)
    await db.log_event(user.id, f"{section}_opened")
    await cb.answer()
    if section == ui.REVIEWS:
        await delivery.send_reviews(bot, user.id, content, config)
        return
    if section == ui.COURSE:
        await delivery.send_course(bot, user.id, content)
        return
    if section == ui.COURSE_REVIEWS:
        await delivery.send_reviews(bot, user.id, content, config, kind=ui.COURSE_REVIEWS)
        return
    text, kb = ui.about_screen(
        content, config.channel_url, config.booking_enabled, config.contact
    )
    await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(ui.SlideCb.filter())
async def on_slide(
    cb: CallbackQuery, callback_data: ui.SlideCb, content: Content, config: Config
) -> None:
    """Стрелки ◀ ▶ под отзывами: та же подпись и кнопки, другая картинка."""
    await cb.answer()
    if callback_data.kind not in (ui.REVIEWS, ui.COURSE_REVIEWS) or not isinstance(
        cb.message, Message
    ):
        return
    try:
        await delivery.show_slide(
            cb.message, content, config, callback_data.kind, callback_data.i
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(ui.NoopCb.filter())
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


# --- прохождение ---

async def _active_session(cb: CallbackQuery, db: Database, session_id: int) -> Session | None:
    session = await db.get_session(session_id)
    if session is None or session.user_id != cb.from_user.id or session.finished_at:
        await cb.answer("Этот тест уже завершён. Нажми /start, чтобы пройти заново.")
        return None
    return session


@router.callback_query(ui.AnswerCb.filter())
async def on_answer(
    cb: CallbackQuery, callback_data: ui.AnswerCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    session = await _active_session(cb, db, callback_data.session)
    if session is None or not isinstance(cb.message, Message):
        return
    # Защита от двойного нажатия и устаревших кнопок.
    if callback_data.q != len(session.answers) or not 1 <= callback_data.v <= 5:
        await cb.answer()
        return

    answers = [*session.answers, callback_data.v]
    await db.save_answers(session.id, answers)

    if len(answers) < len(content.questions):
        text, kb = ui.question_screen(content, session.id, len(answers))
        await safe_edit(cb.message, text, kb)
        await cb.answer()
        return

    result = score(content.questions, answers, content.threshold)
    await db.finish_session(session.id, result.anxiety, result.avoidance, result.type.value)
    await db.log_event(cb.from_user.id, "quiz_finished")
    await cb.answer(content.t("calculating"))
    delivery.schedule_final_cta(bot, db, content, config, cb.from_user.id)

    # Уже подписан — сразу полный результат, без гейта.
    if await is_subscribed(bot, config.channel, cb.from_user.id):
        await db.mark(session.id, "unlocked_at")
        await db.log_event(cb.from_user.id, "unlocked_direct")
        await safe_delete(cb.message)
        await delivery.send_full_result(
            bot, cb.from_user.id, content, config,
            result.type, result.anxiety, result.avoidance,
        )
        return
    await db.mark(session.id, "gate_shown_at")
    await db.log_event(cb.from_user.id, "gate_shown")
    text, kb = ui.gate_screen(
        content, config.channel, config.channel_url,
        result.type, result.anxiety, result.avoidance,
    )
    await safe_edit(cb.message, text, kb)


@router.callback_query(ui.BackCb.filter())
async def on_back(
    cb: CallbackQuery, callback_data: ui.BackCb, db: Database, content: Content
) -> None:
    session = await _active_session(cb, db, callback_data.session)
    if session is None or not isinstance(cb.message, Message):
        return
    if callback_data.q != len(session.answers) or not session.answers:
        await cb.answer()
        return
    answers = session.answers[:-1]
    await db.save_answers(session.id, answers)
    text, kb = ui.question_screen(content, session.id, len(answers))
    await safe_edit(cb.message, text, kb)
    await cb.answer()


# --- гейт и результат ---

@router.callback_query(ui.CheckCb.filter())
async def on_check(
    cb: CallbackQuery, db: Database, content: Content, config: Config, bot: Bot
) -> None:
    session = await db.latest_finished_session(cb.from_user.id)
    if session is None or session.result_type is None:
        await cb.answer("Сначала пройди тест — /start")
        return
    if not await is_subscribed(bot, config.channel, cb.from_user.id):
        await cb.answer(content.t("not_subscribed", channel=config.channel), show_alert=True)
        return

    first_unlock = session.unlocked_at is None
    await db.mark(session.id, "unlocked_at")
    if first_unlock:
        await db.log_event(cb.from_user.id, "unlocked")
    await cb.answer()
    if isinstance(cb.message, Message):
        await safe_delete(cb.message)
    await delivery.send_full_result(
        bot, cb.from_user.id, content, config,
        AttachmentType(session.result_type), session.anxiety or 0, session.avoidance or 0,
    )


@router.message(Command("result"))
async def cmd_result(
    message: Message, db: Database, content: Content, config: Config, bot: Bot
) -> None:
    user = message.from_user
    if user is None:
        return
    session = await db.latest_finished_session(user.id)
    if session is None or session.result_type is None:
        await message.answer("Ты ещё не проходил(а) тест. Нажми /start")
        return
    rtype = AttachmentType(session.result_type)
    anxiety, avoidance = session.anxiety or 0, session.avoidance or 0
    if session.unlocked_at or await is_subscribed(bot, config.channel, user.id):
        await db.mark(session.id, "unlocked_at")
        await message.answer(content.t("already_full"))
        await delivery.send_full_result(bot, user.id, content, config, rtype, anxiety, avoidance)
        return
    text, kb = ui.gate_screen(
        content, config.channel, config.channel_url, rtype, anxiety, avoidance
    )
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# --- админка ---

TYPE_NAMES = {
    "secure": "безопасная", "anxious": "тревожная",
    "avoidant": "избегающая", "disorganized": "дезорганизованная",
}


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


ADMIN_HINT = "Ты админ — кнопка «📊 Статистика» внизу экрана."


def is_admin(user: User | None, config: Config) -> bool:
    return user is not None and user.id in config.admin_ids


def blocked_line(s: dict[str, object]) -> str:
    """Всего за всё время; разблокировавшие тоже входят в число и показаны отдельно."""
    blocked, ever, users = s["blocked"], s["blocked_ever"], s["users"]
    assert isinstance(blocked, int) and isinstance(ever, int) and isinstance(users, int)
    line = f"Заблокировали бота: {ever} ({pct(ever, users)})"
    if ever > blocked:
        line += f", из них вернулись: {ever - blocked}"
    return line


async def stats_text(db: Database) -> str:
    s = await db.stats()
    users, started, finished = s["users"], s["started"], s["finished"]
    gate, via_gate = s["gate_shown"], s["unlocked_via_gate"]
    assert isinstance(users, int) and isinstance(started, int) and isinstance(finished, int)
    assert isinstance(gate, int) and isinstance(via_gate, int)

    lines = [
        "<b>📊 Воронка</b>",
        f"Пользователей: {users} (за 24 ч: {s['users_24h']})",
        f"Начали тест: {started} ({pct(started, users)})",
        f"Закончили: {finished} ({pct(finished, started)})",
        f"Увидели гейт: {gate}",
        f"Подписались через гейт: {via_gate} ({pct(via_gate, gate)} от гейта)",
        f"Открыли результат всего: {s['unlocked']}",
        f"Получили напоминание: {s['reminded']}",
        f"Получили дневник: {s['diary_sent']}",
        f"Открыли «Кто такая Дарья?»: {s['about_opened']}",
        f"Открыли отзывы: {s['reviews_opened']}",
        f"Открыли курс: {s['course_opened']}",
        f"Открыли отзывы из курса: {s['course_reviews_opened']}",
        blocked_line(s),
        "",
        "<b>Источники</b> (/start &lt;метка&gt;)",
    ]
    by_source = s["by_source"]
    assert isinstance(by_source, list)
    lines += [f"• {html.escape(src)}: {n}" for src, n in by_source] or ["—"]
    lines += ["", "<b>Типы</b>"]
    by_type = s["by_type"]
    assert isinstance(by_type, list)
    lines += [f"• {TYPE_NAMES.get(t, t)}: {n}" for t, n in by_type] or ["—"]
    return "\n".join(lines)


@router.message(Command("stats"))
@router.message(F.text == ui.STATS_BUTTON)
async def cmd_stats(message: Message, db: Database, config: Config) -> None:
    if not is_admin(message.from_user, config):
        return
    await message.answer(await stats_text(db), reply_markup=ui.stats_keyboard())


@router.message(Command("admin"))
async def cmd_admin(message: Message, config: Config) -> None:
    """Вернуть кнопку статистики, если её скрыли."""
    if not is_admin(message.from_user, config):
        return
    await message.answer(ADMIN_HINT, reply_markup=ui.admin_keyboard())


CHANNEL_POST_KEY = "channel_post_id"


@router.message(Command("pin_post"))
async def cmd_pin_post(
    message: Message, db: Database, content: Content, config: Config, bot: Bot
) -> None:
    """Опубликовать (или обновить) пост с кнопкой записи в канале и закрепить его."""
    if not is_admin(message.from_user, config):
        return
    me = await bot.me()
    text, kb = ui.channel_post(content, me.username or "")
    chat = config.channel
    try:
        post_id = await _update_channel_post(bot, db, chat, text, kb)
        await bot.pin_chat_message(chat, post_id, disable_notification=True)
    except TelegramAPIError as e:
        log.warning("pin_post failed: %s", e)
        await message.answer(
            f"Не получилось: {html.escape(str(e))}\n\n"
            f"Проверь, что бот — админ {chat} с правами «Публикация сообщений» "
            "и «Закрепление сообщений»."
        )
        return
    await message.answer(f"Готово: пост закреплён в {chat} ✅")


async def _update_channel_post(
    bot: Bot, db: Database, chat: str, text: str, kb: InlineKeyboardMarkup
) -> int:
    """Отредактировать прежний пост, если он ещё есть; иначе опубликовать новый."""
    old = await db.get_setting(CHANNEL_POST_KEY)
    if old:
        try:
            await bot.edit_message_text(
                text, chat_id=chat, message_id=int(old),
                reply_markup=kb, disable_web_page_preview=True,
            )
            return int(old)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return int(old)
            log.info("old channel post %s not editable (%s), sending a new one", old, e)
    sent = await bot.send_message(chat, text, reply_markup=kb, disable_web_page_preview=True)
    await db.set_setting(CHANNEL_POST_KEY, str(sent.message_id))
    return sent.message_id


@router.message(Command("course_post"))
async def cmd_course_post(
    message: Message, content: Content, config: Config, bot: Bot
) -> None:
    """Превью поста о курсе админу; в канал — только по кнопке «Опубликовать»."""
    if not is_admin(message.from_user, config):
        return
    me = await bot.me()
    caption, kb = ui.course_post(content, me.username or "")
    await delivery.send_photo_post(bot, message.chat.id, content.course_path, caption, kb)
    await message.answer(
        f"☝️ Так пост будет выглядеть в {config.channel}. Публикуем?",
        reply_markup=ui.course_post_confirm_keyboard(),
    )


@router.callback_query(ui.CoursePostCb.filter())
async def on_course_post_publish(
    cb: CallbackQuery, content: Content, config: Config, bot: Bot
) -> None:
    if not is_admin(cb.from_user, config):
        await cb.answer()
        return
    await cb.answer()
    me = await bot.me()
    caption, kb = ui.course_post(content, me.username or "")
    chat = config.channel
    try:
        await delivery.send_photo_post(bot, chat, content.course_path, caption, kb)
    except TelegramAPIError as e:
        log.warning("course_post failed: %s", e)
        if isinstance(cb.message, Message):
            await cb.message.answer(
                f"Не получилось: {html.escape(str(e))}\n\n"
                f"Проверь, что бот — админ {chat} с правом «Публикация сообщений»."
            )
        return
    if isinstance(cb.message, Message):
        # Убираем кнопку, чтобы случайно не опубликовать второй раз.
        with contextlib.suppress(TelegramBadRequest):
            await cb.message.edit_text(f"Готово: пост о курсе опубликован в {chat} ✅")


@router.callback_query(ui.StatsCb.filter())
async def on_stats_refresh(cb: CallbackQuery, db: Database, config: Config) -> None:
    if not is_admin(cb.from_user, config):
        await cb.answer()
        return
    if isinstance(cb.message, Message):
        await safe_edit(cb.message, await stats_text(db), ui.stats_keyboard())
    await cb.answer("Обновлено")
