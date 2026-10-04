"""Проверка подписки на канал. Бот должен быть администратором канала."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

SUBSCRIBED = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def is_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """True, если пользователь в канале.

    Если Telegram не дал ответ (бот не админ, канал переименован и т.п.) —
    пропускаем пользователя дальше (fail-open): лучше отдать результат без
    проверки, чем запереть человека. Ошибку пишем в лог, её видно в /stats.
    """
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except TelegramAPIError:
        log.exception("Subscription check failed for %s in %s — failing open", user_id, channel)
        return True
    if member.status in SUBSCRIBED:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def self_check(bot: Bot, channel: str) -> str | None:
    """Проверка при старте: бот видит канал и является в нём администратором."""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id=channel, user_id=me.id)
    except TelegramAPIError as e:
        return f"не удаётся получить канал {channel}: {e}"
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return f"бот не администратор в {channel} (статус: {member.status})"
    return None
