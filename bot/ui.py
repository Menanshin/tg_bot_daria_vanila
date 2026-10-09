"""Тексты сообщений и клавиатуры."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.content import Content
from bot.scoring import AttachmentType

BAR_LEN = 10
SCALE_MIN, SCALE_MAX = 5, 25


class StartCb(CallbackData, prefix="go"):
    pass


class AnswerCb(CallbackData, prefix="a"):
    session: int
    q: int  # индекс вопроса, 0-based
    v: int


class BackCb(CallbackData, prefix="b"):
    session: int
    q: int


class CheckCb(CallbackData, prefix="chk"):
    pass


class RestartCb(CallbackData, prefix="re"):
    pass


class StatsCb(CallbackData, prefix="st"):
    pass


STATS_BUTTON = "📊 Статистика"


def progress_bar(done: int, total: int) -> str:
    return "▰" * done + "▱" * (total - done)


def scale_bar(score: int, threshold: int) -> str:
    filled = round((score - SCALE_MIN) / (SCALE_MAX - SCALE_MIN) * BAR_LEN)
    filled = max(0, min(BAR_LEN, filled))
    level = "высокая" if score > threshold else "низкая"
    return f"{'■' * filled}{'□' * (BAR_LEN - filled)} {level}"


# --- экраны ---

def start_screen(content: Content) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=content.t("start_button"), callback_data=StartCb().pack())
    ]])
    return content.t("start"), kb


def question_screen(
    content: Content, session_id: int, index: int
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(content.questions)
    question = content.questions[index]
    text = content.t(
        "question",
        current=index + 1,
        total=total,
        progress=progress_bar(index, total),
        text=question.text,
    )
    rows = [[
        InlineKeyboardButton(
            text=str(v), callback_data=AnswerCb(session=session_id, q=index, v=v).pack()
        )
        for v in range(1, 6)
    ]]
    if index > 0:
        rows.append([InlineKeyboardButton(
            text=content.t("back_button"),
            callback_data=BackCb(session=session_id, q=index).pack(),
        )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def gate_screen(
    content: Content, channel: str, channel_url: str,
    result_type: AttachmentType, anxiety: int, avoidance: int,
) -> tuple[str, InlineKeyboardMarkup]:
    tt = content.types[result_type]
    text = content.t(
        "gate",
        title=tt.title,
        description=tt.description,
        anxiety_bar=scale_bar(anxiety, content.threshold),
        avoidance_bar=scale_bar(avoidance, content.threshold),
        channel=channel,
    )
    return text, gate_keyboard(content, channel_url)


def gate_keyboard(content: Content, channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=content.t("subscribe_button"), url=channel_url)],
        [InlineKeyboardButton(text=content.t("check_button"), callback_data=CheckCb().pack())],
    ])


def share_url(content: Content, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start=share"
    text = quote(content.t("share_text"))
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={text}"


def full_screen(
    content: Content, channel: str, bot_username: str,
    result_type: AttachmentType, anxiety: int, avoidance: int,
) -> tuple[str, InlineKeyboardMarkup]:
    tt = content.types[result_type]
    text = "\n\n".join([
        content.t("full_header", title=tt.title, description=tt.description),
        (
            f"Тревожность: {scale_bar(anxiety, content.threshold)}\n"
            f"Избегание: {scale_bar(avoidance, content.threshold)}"
        ),
        tt.full,
        content.t("full_footer", channel=channel),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=content.t("share_button"), url=share_url(content, bot_username)
        )],
        [InlineKeyboardButton(text=content.t("restart_button"), callback_data=RestartCb().pack())],
    ])
    return text, kb


def final_cta_screen(content: Content, contact: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Финальное сообщение с кнопкой в личку Даши. Без CONTACT — без кнопки."""
    if not contact:
        return content.t("final_cta"), None
    url = f"https://t.me/{contact}?text={quote(content.t('final_cta_message'))}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=content.t("final_cta_button"), url=url)
    ]])
    return content.t("final_cta"), kb


# --- админка ---

def admin_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная кнопка под полем ввода — видят только админы."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=STATS_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=StatsCb().pack())
    ]])
