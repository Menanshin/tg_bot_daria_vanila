"""Запись на созвон: слоты, окно между записями, гонки, подтверждение Даши, отмена."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Message

from bot import booking as bk
from bot import booking_handlers as bh
from bot import booking_ui as bui
from bot.booking import Kind, Status
from bot.config import Config
from bot.content import load_content
from bot.db import BookResult, Database

CONTENT = load_content()
MSK = ZoneInfo("Europe/Moscow")
BKK = ZoneInfo("Asia/Bangkok")
OWNER_ID = 500
CLIENT, OTHER = 42, 43

# Понедельник 12 октября 2026, 16:00 по Бангкоку = 12:00 МСК.
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def msk(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(UTC)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=NOW)
    monkeypatch.setattr(bk, "utcnow", lambda: state.now)
    return state


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    # Каждый день 10:00–23:00 по Бангкоку = 06:00–19:00 МСК.
    for wd in range(7):
        await d.set_weekday(wd, [(600, 1380)])
    yield d
    await d.close()


@pytest.fixture
def config():
    return Config(
        bot_token="x", channel="@vanillapropsy", admin_ids=frozenset(),
        db_path=":memory:", reminder_hours=0, log_level="INFO",
        booking_owner_id=OWNER_ID,
    )


async def free(db: Database, kind: Kind, now: datetime = NOW) -> list[datetime]:
    schedule = await db.load_schedule(BKK)
    busy = await db.busy_cells(now, now + timedelta(days=32))
    return bk.free_starts(schedule, kind, now, busy)


# --- расписание и слоты (чистая логика) ---

def test_parse_intervals():
    assert bk.parse_intervals("10:00-14:00, 16-20") == [(600, 840), (960, 1200)]
    assert bk.parse_intervals("Выходной") == []
    assert bk.parse_intervals("9:30 – 24:00") == [(570, 1440)]
    for bad in ("10-9", "abc", "10:00-14:00, 13:00-15:00", "25:00-26:00", ""):
        with pytest.raises(bk.ScheduleError):
            bk.parse_intervals(bad)


async def test_slots_are_shown_in_moscow_time(db):
    starts = await free(db, Kind.FULL)
    tomorrow = [s for s in starts if s.astimezone(MSK).date().day == 13]
    # Бангкок 10:00–23:00 → МСК 06:00–19:00, последний часовой слот в 18:00.
    assert tomorrow[0] == msk(13, 6)
    assert tomorrow[-1] == msk(13, 18)
    assert all(s.astimezone(MSK).minute in (0, 30) for s in starts)


async def test_minimum_two_hours_before_start(db):
    starts = await free(db, Kind.TRIAL)
    # Сейчас 12:00 МСК — первое окно сегодня не раньше 14:00 МСК.
    assert min(starts) == msk(12, 14)
    assert msk(12, 13, 30) not in starts


async def test_horizon_is_one_month(db):
    starts = await free(db, Kind.FULL)
    assert max(starts) <= NOW + timedelta(days=30)
    assert max(starts) >= NOW + timedelta(days=29)


async def test_day_off_and_special_dates(db):
    sunday = datetime(2026, 10, 18).date()
    await db.set_date(sunday, [])
    starts = await free(db, Kind.FULL)
    assert not [s for s in starts if s.astimezone(BKK).date() == sunday]
    await db.set_date(sunday, None)
    starts = await free(db, Kind.FULL)
    assert [s for s in starts if s.astimezone(BKK).date() == sunday]


async def test_hour_gap_between_calls(db):
    result, _ = await db.create_booking(CLIENT, Kind.FULL, msk(13, 12), NOW)
    assert result is BookResult.OK
    full, trial = await free(db, Kind.FULL), await free(db, Kind.TRIAL)
    # Звонок 12:00–13:00, дальше час тишины до 14:00.
    assert msk(13, 12, 30) not in trial
    assert msk(13, 13, 30) not in trial
    assert msk(13, 14) in full
    # До него: часовой звонок должен закончиться не позже 11:00.
    assert msk(13, 10) in full
    assert msk(13, 10, 30) not in full
    assert msk(13, 10, 30) in trial      # 10:30–10:45, +час = 11:45 — ок
    assert msk(13, 11) not in trial      # 11:00–11:15, +час = 12:15 — пересечение


# --- атомарность ---

async def test_same_slot_concurrently_only_one_wins(db):
    results = await asyncio.gather(*[
        db.create_booking(uid, Kind.FULL, msk(13, 15), NOW) for uid in range(100, 110)
    ])
    assert sum(r is BookResult.OK for r, _ in results) == 1
    assert sum(r is BookResult.TAKEN for r, _ in results) == 9


async def test_overlapping_kinds_concurrently(db):
    (r1, _), (r2, _) = await asyncio.gather(
        db.create_booking(CLIENT, Kind.FULL, msk(13, 15), NOW),
        db.create_booking(OTHER, Kind.TRIAL, msk(13, 15, 30), NOW),
    )
    assert sorted([r1, r2]) == sorted([BookResult.OK, BookResult.TAKEN])


async def test_trial_once_and_one_active_per_kind(db, clock):
    r, b = await db.create_booking(CLIENT, Kind.TRIAL, msk(13, 9), NOW)
    assert r is BookResult.OK
    r, _ = await db.create_booking(CLIENT, Kind.TRIAL, msk(14, 9), NOW)
    assert r is BookResult.HAS_ACTIVE
    # Полноценную можно параллельно с пробной.
    r, _ = await db.create_booking(CLIENT, Kind.FULL, msk(15, 9), NOW)
    assert r is BookResult.OK
    # Пробная прошла — второй раз нельзя.
    assert await db.transition(b.id, (Status.PENDING,), Status.CONFIRMED, NOW)
    later = msk(13, 10)
    r, _ = await db.create_booking(CLIENT, Kind.TRIAL, msk(16, 9), later)
    assert r is BookResult.TRIAL_USED
    # Отклонённая пробная не считается использованной.
    r, b2 = await db.create_booking(OTHER, Kind.TRIAL, msk(13, 15), NOW)
    assert await db.transition(b2.id, (Status.PENDING,), Status.DECLINED, NOW)
    r, _ = await db.create_booking(OTHER, Kind.TRIAL, msk(14, 15), NOW)
    assert r is BookResult.OK


async def test_cancel_frees_slot(db):
    _, b = await db.create_booking(CLIENT, Kind.FULL, msk(13, 15), NOW)
    assert msk(13, 15) not in await free(db, Kind.FULL)
    assert await db.transition(b.id, bk.ACTIVE, Status.CANCELLED, NOW)
    assert msk(13, 15) in await free(db, Kind.FULL)


async def test_cannot_confirm_after_hold_expired(db):
    _, b = await db.create_booking(CLIENT, Kind.FULL, msk(20, 15), NOW)
    assert b.hold_until == NOW + timedelta(hours=24)
    late = NOW + timedelta(hours=25)
    ok = await db.transition(b.id, (Status.PENDING,), Status.CONFIRMED, late, hold_active=True)
    assert not ok


async def test_hold_never_outlives_the_meeting(db):
    _, b = await db.create_booking(CLIENT, Kind.TRIAL, msk(12, 15), NOW)
    assert b.hold_until == msk(12, 15)


# --- хендлеры ---

def make_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=777))
    bot.edit_message_text = AsyncMock()
    return bot


def make_cb(user_id: int = CLIENT):
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=user_id, username="client", first_name="Аня")
    cb.message = message
    cb.answer = AsyncMock()
    return cb


async def book(db, config, bot, start, kind=Kind.FULL, user_id=CLIENT):
    cb = make_cb(user_id)
    data = bui.BookConfirmCb(kind=kind, at=bk.ts(start))
    await bh.on_confirm(cb, data, db, CONTENT, config, bot)
    return cb


async def test_client_books_and_owner_is_notified(db, config, clock):
    bot = make_bot()
    cb = await book(db, config, bot, msk(13, 15))
    assert "Заявка отправлена" in cb.message.edit_text.call_args.args[0]
    kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[-1][0].url == config.channel_url  # подписка после записи
    owner_call = bot.send_message.call_args
    assert owner_call.args[0] == OWNER_ID
    text = owner_call.args[1]
    assert "19:00–20:00 (твоё время)" in text  # 15:00 МСК = 19:00 Бангкок
    assert "15:00 МСК" in text
    (b,) = await db.user_bookings(CLIENT, NOW)
    assert b.owner_msg_id == 777


async def test_booking_too_close_is_rejected(db, config, clock):
    bot = make_bot()
    cb = await book(db, config, bot, msk(12, 13))  # через час
    assert "заняли" in cb.message.edit_text.call_args.args[0]
    assert not await db.user_bookings(CLIENT, NOW)
    bot.send_message.assert_not_awaited()


async def test_second_client_gets_taken(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(13, 15))
    cb = await book(db, config, bot, msk(13, 15, 30), kind=Kind.TRIAL, user_id=OTHER)
    assert "заняли" in cb.message.edit_text.call_args.args[0]


async def test_owner_confirms_and_client_is_notified(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(13, 15))
    (b,) = await db.user_bookings(CLIENT, NOW)
    bot.send_message.reset_mock()
    owner_cb = make_cb(OWNER_ID)
    await bh.on_owner_decision(owner_cb, bui.OwnerDecisionCb(id=b.id, ok=True),
                               db, CONTENT, config, bot)
    assert (await db.get_booking(b.id)).status is Status.CONFIRMED
    assert bot.send_message.call_args.args[0] == CLIENT
    assert "подтвердила" in bot.send_message.call_args.args[1]
    kb = bot.send_message.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[-1][0].url == config.channel_url


async def test_stranger_cannot_decide(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(13, 15))
    (b,) = await db.user_bookings(CLIENT, NOW)
    await bh.on_owner_decision(make_cb(OTHER), bui.OwnerDecisionCb(id=b.id, ok=True),
                               db, CONTENT, config, bot)
    assert (await db.get_booking(b.id)).status is Status.PENDING


async def test_client_cancel_rules(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(13, 15))
    (b,) = await db.user_bookings(CLIENT, NOW)

    # Чужую запись отменить нельзя.
    await bh.on_client_cancel(make_cb(OTHER), bui.BookCancelCb(id=b.id, sure=True),
                              db, CONTENT, config, bot)
    assert (await db.get_booking(b.id)).status is Status.PENDING

    # За 20 минут до начала — уже нельзя.
    clock.now = msk(13, 14, 40)
    cb = make_cb()
    await bh.on_client_cancel(cb, bui.BookCancelCb(id=b.id, sure=True),
                              db, CONTENT, config, bot)
    assert "30 минут" in cb.answer.call_args.args[0]
    assert (await db.get_booking(b.id)).status is Status.PENDING

    # За 30 минут — можно, Даша получает уведомление.
    clock.now = msk(13, 14, 30)
    bot.send_message.reset_mock()
    await bh.on_client_cancel(make_cb(), bui.BookCancelCb(id=b.id, sure=True),
                              db, CONTENT, config, bot)
    assert (await db.get_booking(b.id)).status is Status.CANCELLED
    assert bot.send_message.call_args.args[0] == OWNER_ID


async def test_pending_expires_after_a_day(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(20, 15))
    (b,) = await db.user_bookings(CLIENT, NOW)
    bot.send_message.reset_mock()

    clock.now = NOW + timedelta(hours=23)
    await bh.process_bookings(bot, db, CONTENT, config)
    assert (await db.get_booking(b.id)).status is Status.PENDING

    clock.now = NOW + timedelta(hours=24)
    await bh.process_bookings(bot, db, CONTENT, config)
    assert (await db.get_booking(b.id)).status is Status.EXPIRED
    recipients = [c.args[0] for c in bot.send_message.call_args_list]
    assert CLIENT in recipients and OWNER_ID in recipients
    assert msk(20, 15) in await free(db, Kind.FULL, clock.now)


async def test_reminders_for_confirmed(db, config, clock):
    bot = make_bot()
    await book(db, config, bot, msk(20, 15))
    (b,) = await db.user_bookings(CLIENT, NOW)
    await db.transition(b.id, (Status.PENDING,), Status.CONFIRMED, NOW)
    bot.send_message.reset_mock()

    clock.now = msk(19, 15)
    await bh.process_bookings(bot, db, CONTENT, config)
    assert "завтра" in bot.send_message.call_args.args[1]

    bot.send_message.reset_mock()
    clock.now = msk(20, 14)
    await bh.process_bookings(bot, db, CONTENT, config)
    assert "Через час" in bot.send_message.call_args.args[1]

    bot.send_message.reset_mock()
    clock.now = msk(20, 14, 30)
    await bh.process_bookings(bot, db, CONTENT, config)
    bot.send_message.assert_not_awaited()


async def test_menu_hides_used_trial(db, config, clock):
    await db.create_booking(CLIENT, Kind.TRIAL, msk(13, 9), NOW)
    text, kb = bui.menu_screen(CONTENT, MSK, await db.user_bookings(CLIENT, NOW),
                               await db.trial_used(CLIENT))
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert not any("Пробная" in lbl for lbl in labels)
    assert any("Консультация" in lbl for lbl in labels)
    assert any("Отменить" in lbl for lbl in labels)
    assert "09:00–09:15 МСК" in text
