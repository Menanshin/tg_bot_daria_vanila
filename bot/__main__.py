"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat

from bot.config import load_config
from bot.content import load_content
from bot.db import Database
from bot.handlers import router
from bot.reminders import reminder_loop
from bot.subscription import self_check

log = logging.getLogger("bot")


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    content = load_content()

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, content=content, config=config)
    dp.include_router(router)

    problem = await self_check(bot, config.channel)
    if problem:
        log.warning("Проверка подписки не будет работать: %s", problem)

    commands = [
        BotCommand(command="start", description="Пройти тест"),
        BotCommand(command="result", description="Мой результат"),
    ]
    await bot.set_my_commands(commands)
    # Админам — ещё и /stats в меню. Если админ ещё не писал боту, Telegram
    # вернёт «chat not found»: меню появится после перезапуска, кнопка — после /start.
    admin_commands = [*commands, BotCommand(command="stats", description="Статистика")]
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest as e:
            log.warning("Не удалось задать меню админу %s: %s", admin_id, e)

    reminders: asyncio.Task[None] | None = None
    if config.reminder_hours > 0:
        reminders = asyncio.create_task(reminder_loop(bot, db, content, config))

    me = await bot.me()
    log.info("Started @%s, channel %s", me.username, config.channel)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if reminders:
            reminders.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reminders
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
