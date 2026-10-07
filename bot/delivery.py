"""Отправка сообщений с файлами: результат с картинкой и дневник-PDF.

Файлы загружаются в Telegram один раз, дальше переиспользуется file_id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import FSInputFile, Message

from bot import ui
from bot.config import Config
from bot.content import Content
from bot.db import Database, now_iso
from bot.scoring import AttachmentType

log = logging.getLogger(__name__)

_file_ids: dict[Path, str] = {}
_pending: set[asyncio.Task[None]] = set()


def _input(path: Path, filename: str | None = None) -> str | FSInputFile:
    return _file_ids.get(path) or FSInputFile(path, filename=filename)


def _remember(path: Path, file_id: str | None) -> None:
    if isinstance(file_id, str):
        _file_ids[path] = file_id


async def send_full_result(
    bot: Bot, chat_id: int, content: Content, config: Config,
    result_type: AttachmentType, anxiety: int, avoidance: int,
) -> None:
    """Картинка типа → полная расшифровка. Финальное сообщение — отдельно, по таймеру."""
    image = content.type_image(result_type)
    try:
        sent = await bot.send_photo(chat_id, _input(image))
        if isinstance(sent, Message) and sent.photo:
            _remember(image, sent.photo[-1].file_id)
    except TelegramForbiddenError:
        raise
    except TelegramAPIError:
        # Картинка — украшение: если не ушла, текст результата всё равно отправляем.
        log.exception("Failed to send result image %s", image.name)

    me = await bot.me()
    text, kb = ui.full_screen(
        content, config.channel, me.username or "", result_type, anxiety, avoidance
    )
    await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


async def send_final_cta(bot: Bot, chat_id: int, content: Content, config: Config) -> None:
    text, kb = ui.final_cta_screen(content, config.contact)
    await bot.send_message(chat_id, text, reply_markup=kb)


async def send_diary(bot: Bot, chat_id: int, content: Content) -> None:
    path = content.diary_path
    sent = await bot.send_document(
        chat_id,
        _input(path, filename=content.t("diary_filename")),
        caption=content.t("diary"),
    )
    if isinstance(sent, Message) and sent.document:
        _remember(path, sent.document.file_id)


async def _diary_later(
    bot: Bot, db: Database, content: Content, user_id: int, delay_sec: float, since: str
) -> None:
    await asyncio.sleep(delay_sec)
    try:
        if await db.has_event(user_id, "diary_sent"):
            return
        if await db.has_event(user_id, "quiz_started", since=since):
            return
        await send_diary(bot, user_id, content)
        await db.log_event(user_id, "diary_sent")
    except TelegramForbiddenError:
        log.info("User %s blocked the bot, skipping diary", user_id)
    except Exception:
        log.exception("Failed to send diary to %s", user_id)


async def _cta_later(
    bot: Bot, db: Database, content: Content, config: Config,
    user_id: int, delay_sec: float, since: str,
) -> None:
    await asyncio.sleep(delay_sec)
    try:
        # Человек мог пройти тест ещё раз за эти минуты — второй раз не шлём.
        if await db.has_event(user_id, "cta_sent", since=since):
            return
        await send_final_cta(bot, user_id, content, config)
        await db.log_event(user_id, "cta_sent")
    except TelegramForbiddenError:
        log.info("User %s blocked the bot, skipping final message", user_id)
    except Exception:
        log.exception("Failed to send final message to %s", user_id)


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def schedule_final_cta(
    bot: Bot, db: Database, content: Content, config: Config, user_id: int
) -> None:
    """Через CTA_DELAY_MINUTES после прохождения теста — сообщение «Пиши «близость»».

    Таймер живёт в памяти: при перезапуске бота несработавшие таймеры теряются.
    0 — отправить сразу (без таймера).
    """
    delay = max(config.cta_delay_minutes, 0) * 60
    _spawn(_cta_later(bot, db, content, config, user_id, delay, now_iso()))


def schedule_diary(
    bot: Bot, db: Database, content: Content, config: Config, user_id: int
) -> None:
    """Через DIARY_DELAY_MINUTES прислать дневник, если тест так и не начат.

    Таймер живёт в памяти: при перезапуске бота несработавшие таймеры теряются.
    """
    if config.diary_delay_minutes <= 0:
        return
    _spawn(_diary_later(bot, db, content, user_id, config.diary_delay_minutes * 60, now_iso()))
