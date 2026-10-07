"""Конфигурация из переменных окружения. Секреты в репозиторий не попадают."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    channel: str
    admin_ids: frozenset[int]
    db_path: str
    reminder_hours: float
    log_level: str
    # Username Даши для кнопки «Написать «близость»». Пусто — кнопка не показывается.
    contact: str = ""
    # Через сколько минут после /start прислать дневник, если тест не начат. 0 — выключено.
    diary_delay_minutes: float = 0

    @property
    def channel_url(self) -> str:
        return f"https://t.me/{self.channel.lstrip('@')}"


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as e:
            raise ConfigError(f"ADMIN_IDS: {part!r} is not a numeric Telegram user id") from e
    return frozenset(ids)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set (see .env.example)")

    channel = os.getenv("CHANNEL", "@vanillapropsy").strip()
    if not channel.startswith("@"):
        channel = "@" + channel

    try:
        reminder_hours = float(os.getenv("REMINDER_HOURS", "24"))
    except ValueError as e:
        raise ConfigError("REMINDER_HOURS must be a number (0 disables reminders)") from e

    try:
        diary_delay_minutes = float(os.getenv("DIARY_DELAY_MINUTES", "3"))
    except ValueError as e:
        raise ConfigError("DIARY_DELAY_MINUTES must be a number (0 disables it)") from e

    contact = os.getenv("CONTACT", "daria_vanilla").strip().lstrip("@")
    if contact.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        contact = contact.split("t.me/", 1)[1].strip("/")

    return Config(
        bot_token=token,
        channel=channel,
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        reminder_hours=reminder_hours,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        contact=contact,
        diary_delay_minutes=diary_delay_minutes,
    )
