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

from bot.booking_ui import book_button
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


class MenuCb(CallbackData, prefix="m"):
    section: str  # about | reviews


ABOUT, REVIEWS = "about", "reviews"


STATS_BUTTON = "📊 Статистика"


def progress_bar(done: int, total: int) -> str:
    return "▰" * done + "▱" * (total - done)


def scale_bar(score: int, threshold: int) -> str:
    filled = round((score - SCALE_MIN) / (SCALE_MAX - SCALE_MIN) * BAR_LEN)
    filled = max(0, min(BAR_LEN, filled))
    level = "высокая" if score > threshold else "низкая"
    return f"{'■' * filled}{'□' * (BAR_LEN - filled)} {level}"


# --- экраны ---

def _menu_button(content: Content, section: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=content.t(f"{section}_button"), callback_data=MenuCb(section=section).pack()
    )


def subscribe_row(content: Content, channel_url: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=content.t("subscribe_button"), url=channel_url)]


def booking_row(content: Content, booking: bool, contact: str) -> list[list[InlineKeyboardButton]]:
    """Кнопка «Онлайн-запись»: запись в боте, а если она выключена — личка Дарьи (CONTACT)."""
    if booking:
        return [[book_button(content)]]
    if contact:
        url = f"https://t.me/{contact}"
        return [[InlineKeyboardButton(text=content.t("book_button"), url=url)]]
    return []


def start_screen(
    content: Content, booking: bool = False, contact: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """Приветствие: тест, онлайн-запись, «Кто такая Дарья?»."""
    rows = [
        [InlineKeyboardButton(text=content.t("start_button"), callback_data=StartCb().pack())],
        *booking_row(content, booking, contact),
        [_menu_button(content, ABOUT)],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = content.t("start")
    # Подпись автора бота — необязательная: удали строку author в texts.yaml, и её не будет.
    if content.texts.get("author", "").strip():
        text += "\n\n" + content.t("author")
    return text, kb


def about_screen(
    content: Content, channel_url: str, booking: bool = False, contact: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *booking_row(content, booking, contact),
        [_menu_button(content, REVIEWS)],
        subscribe_row(content, channel_url),
    ])
    return content.t("about"), kb


def reviews_screen(
    content: Content, channel_url: str, booking: bool = False, contact: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *booking_row(content, booking, contact),
        subscribe_row(content, channel_url),
    ])
    return content.t("reviews"), kb


def quiz_intro_text(content: Content) -> str:
    return content.t("quiz_intro") + "\n\n" + content.t("scale_intro")


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
        subscribe_row(content, channel_url),
        [InlineKeyboardButton(text=content.t("check_button"), callback_data=CheckCb().pack())],
    ])


def share_url(content: Content, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start=share"
    text = quote(content.t("share_text"))
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={text}"


def full_screen(
    content: Content, channel: str, channel_url: str, bot_username: str,
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
        subscribe_row(content, channel_url),
    ])
    return text, kb


def final_cta_screen(
    content: Content, contact: str, channel_url: str, booking: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Финальное сообщение: запись на созвон, личка Дарьи (если задан CONTACT), подписка."""
    rows: list[list[InlineKeyboardButton]] = []
    if booking:
        rows.append([book_button(content)])
    if contact:
        url = f"https://t.me/{contact}?text={quote(content.t('final_cta_message'))}"
        rows.append([InlineKeyboardButton(text=content.t("final_cta_button"), url=url)])
    rows.append(subscribe_row(content, channel_url))
    return content.t("final_cta"), InlineKeyboardMarkup(inline_keyboard=rows)


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


# --- закреп в канале ---

BOOK_DEEP_LINK = "book"   # t.me/<бот>?start=book — сразу меню записи
POST_DEEP_LINK = "pin"    # t.me/<бот>?start=pin — обычный старт, источник «pin» в /stats


def channel_post(content: Content, bot_username: str) -> tuple[str, InlineKeyboardMarkup]:
    """Пост для закрепа: кнопки ведут в бота по deep link (inline-кнопки в канале — только url)."""
    base = f"https://t.me/{bot_username}?start="
    rows = [[InlineKeyboardButton(
        text=content.t("channel_post_book_button"), url=base + BOOK_DEEP_LINK
    )]]
    bot_button = content.t("channel_post_bot_button")
    if bot_button:
        rows.append([InlineKeyboardButton(text=bot_button, url=base + POST_DEEP_LINK)])
    return content.t("channel_post"), InlineKeyboardMarkup(inline_keyboard=rows)
