"""Одно напоминание тем, кто увидел гейт, но не подписался."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot import ui
from bot.config import Config
from bot.content import Content
from bot.db import Database
from bot.scoring import AttachmentType
from bot.subscription import is_subscribed

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 600
SEND_DELAY_SEC = 0.1  # ~10 сообщений/сек, с запасом от лимитов Telegram


async def send_due_reminders(bot: Bot, db: Database, content: Content, config: Config) -> int:
    sent = 0
    for session in await db.sessions_to_remind(config.reminder_hours):
        # Подписался сам, но не нажал «проверить» — не дёргаем.
        if await is_subscribed(bot, config.channel, session.user_id):
            await db.mark(session.id, "reminded_at")
            continue
        title = content.types[AttachmentType(session.result_type or "secure")].title
        try:
            await bot.send_message(
                session.user_id,
                content.t("reminder", title=title, channel=config.channel),
                reply_markup=ui.gate_keyboard(content, config.channel_url),
            )
            sent += 1
            await db.log_event(session.user_id, "reminder_sent")
        except TelegramForbiddenError:
            log.info("User %s blocked the bot, skipping reminder", session.user_id)
        except TelegramRetryAfter as e:
            log.warning("Flood control, sleeping %s s", e.retry_after)
            await asyncio.sleep(e.retry_after)
            continue  # попробуем в следующий проход
        await db.mark(session.id, "reminded_at")
        await asyncio.sleep(SEND_DELAY_SEC)
    return sent


async def reminder_loop(bot: Bot, db: Database, content: Content, config: Config) -> None:
    log.info("Reminders enabled: %s h after the gate", config.reminder_hours)
    while True:
        try:
            sent = await send_due_reminders(bot, db, content, config)
            if sent:
                log.info("Sent %d reminders", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reminder pass failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)
