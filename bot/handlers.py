"""Хендлеры: /start, прохождение теста, гейт подписки, результат, /stats."""

from __future__ import annotations

import contextlib
import html
import logging
import re

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

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


# --- /start ---

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
    text, kb = ui.start_screen(content)
    await message.answer(text, reply_markup=kb)
    delivery.schedule_diary(bot, db, content, config, user.id)


@router.callback_query(ui.StartCb.filter())
async def on_start_quiz(cb: CallbackQuery, db: Database, content: Content) -> None:
    await _begin_quiz(cb, db, content)


@router.callback_query(ui.RestartCb.filter())
async def on_restart(cb: CallbackQuery, db: Database, content: Content) -> None:
    await _begin_quiz(cb, db, content)


async def _begin_quiz(cb: CallbackQuery, db: Database, content: Content) -> None:
    # Приветствие/результат остаются в чате: ниже — шкала ответов, под ней вопросы
    # (одно сообщение, которое редактируется).
    user = cb.from_user
    await db.upsert_user(user.id, user.username, None)
    session = await db.new_session(user.id)
    await db.log_event(user.id, "quiz_started")
    text, kb = ui.question_screen(content, session.id, 0)
    if cb.message is not None:
        await cb.message.answer(content.t("scale_intro"))
        await cb.message.answer(text, reply_markup=kb)
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


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, config: Config) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_ids:
        return
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
    await message.answer("\n".join(lines))
