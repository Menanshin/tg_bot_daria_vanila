"""Запись на созвон: клиентская часть (/book), решения и расписание Дарьи, фоновые задачи."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from bot import booking as bk
from bot import booking_ui as bui
from bot.booking import ACTIVE, Kind, Status
from bot.config import Config
from bot.content import Content
from bot.db import Booking, BookResult, Database

log = logging.getLogger(__name__)
router = Router(name="booking")

POLL_INTERVAL_SEC = 60


class SchedInput(StatesGroup):
    weekday = State()
    date = State()


# --- общее ---

async def _edit(message: Message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _send(
    bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Отправка, которая не роняет обработчик: человек мог заблокировать бота."""
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb,
                                      disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot", chat_id)
    except TelegramAPIError:
        log.exception("Failed to send booking message to %s", chat_id)
    return None


def is_owner(user: User | None, config: Config) -> bool:
    """Расписанием и заявками управляет Дарья; админы — тоже (на случай помощи)."""
    return user is not None and (
        user.id == config.booking_owner_id or user.id in config.admin_ids
    )


async def free_starts(db: Database, config: Config, kind: Kind, now: datetime) -> list[datetime]:
    schedule = await db.load_schedule(config.owner_tz)
    busy = await db.busy_cells(now, now + bk.HORIZON + bk.GAP + timedelta(days=1))
    return bk.free_starts(schedule, kind, now, busy)


async def _client_link(db: Database, user_id: int) -> str:
    return bui.client_link(user_id, await db.username(user_id))


async def _owner_text(db: Database, config: Config, b: Booking, header: str) -> str:
    return bui.owner_booking_text(
        b, await _client_link(db, b.user_id), config.owner_tz, config.client_tz, header
    )


async def _update_owner_message(bot: Bot, db: Database, config: Config, b: Booking) -> None:
    """Обновить исходное уведомление у Дарьи: убрать кнопки, показать итог."""
    if not b.owner_msg_id:
        return
    text = await _owner_text(db, config, b, "Заявка на созвон")
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_text(
            text, chat_id=config.booking_owner_id, message_id=b.owner_msg_id,
            reply_markup=None, disable_web_page_preview=True,
        )


# --- клиент ---

async def _menu(db: Database, content: Content, config: Config, user_id: int) -> bui.Screen:
    now = bk.utcnow()
    return bui.menu_screen(
        content, config.client_tz,
        await db.user_bookings(user_id, now), await db.trial_used(user_id),
    )


@router.message(Command("book"))
async def cmd_book(message: Message, db: Database, content: Content, config: Config) -> None:
    user = message.from_user
    if user is None:
        return
    if not config.booking_enabled:
        await message.answer(content.t("book_unavailable"))
        return
    await db.upsert_user(user.id, user.username, None)
    await db.log_event(user.id, "booking_menu")
    text, kb = await _menu(db, content, config, user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(bui.BookMenuCb.filter())
async def on_menu(
    cb: CallbackQuery, callback_data: bui.BookMenuCb,
    db: Database, content: Content, config: Config,
) -> None:
    if not config.booking_enabled:
        await cb.answer(content.t("book_unavailable"), show_alert=True)
        return
    await db.upsert_user(cb.from_user.id, cb.from_user.username, None)
    text, kb = await _menu(db, content, config, cb.from_user.id)
    if isinstance(cb.message, Message):
        if callback_data.edit:
            await _edit(cb.message, text, kb)
        else:
            # Кнопка из финального сообщения — меню новым сообщением, исходное не трогаем.
            await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


async def _kind_blocked(
    cb: CallbackQuery, db: Database, content: Content, kind: Kind, now: datetime
) -> bool:
    """Нельзя выбирать время, если запись этого типа уже есть или пробная использована."""
    bookings = await db.user_bookings(cb.from_user.id, now)
    if any(b.kind is kind for b in bookings):
        await cb.answer(content.t("book_has_active"), show_alert=True)
        return True
    if kind is Kind.TRIAL and await db.trial_used(cb.from_user.id):
        await cb.answer(content.t("book_trial_used"), show_alert=True)
        return True
    return False


@router.callback_query(bui.BookKindCb.filter())
async def on_kind(
    cb: CallbackQuery, callback_data: bui.BookKindCb,
    db: Database, content: Content, config: Config,
) -> None:
    if not config.booking_enabled or not isinstance(cb.message, Message):
        await cb.answer()
        return
    now = bk.utcnow()
    if await _kind_blocked(cb, db, content, callback_data.kind, now):
        return
    starts = await free_starts(db, config, callback_data.kind, now)
    days = bk.group_by_day(starts, config.client_tz)
    text, kb = bui.days_screen(content, callback_data.kind, days, callback_data.page)
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.BookDayCb.filter())
async def on_day(
    cb: CallbackQuery, callback_data: bui.BookDayCb,
    db: Database, content: Content, config: Config,
) -> None:
    if not config.booking_enabled or not isinstance(cb.message, Message):
        await cb.answer()
        return
    now = bk.utcnow()
    day = bui.parse_day(callback_data.day)
    starts = await free_starts(db, config, callback_data.kind, now)
    days = bk.group_by_day(starts, config.client_tz)
    text, kb = bui.times_screen(
        content, callback_data.kind, day, days.get(day, []), config.client_tz,
        bui.page_of(days, day),
    )
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.BookTimeCb.filter())
async def on_time(
    cb: CallbackQuery, callback_data: bui.BookTimeCb,
    db: Database, content: Content, config: Config,
) -> None:
    if not config.booking_enabled or not isinstance(cb.message, Message):
        await cb.answer()
        return
    start = bk.from_ts(callback_data.at)
    text, kb = bui.confirm_screen(content, callback_data.kind, start, config.client_tz)
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.BookConfirmCb.filter())
async def on_confirm(
    cb: CallbackQuery, callback_data: bui.BookConfirmCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    if not config.booking_enabled or not isinstance(cb.message, Message):
        await cb.answer()
        return
    kind, start, now = callback_data.kind, bk.from_ts(callback_data.at), bk.utcnow()
    user = cb.from_user
    day = start.astimezone(config.client_tz).date()
    retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
        bui.button("← Выбрать другое время", bui.BookDayCb(kind=kind, day=bui.day_key(day)))
    ]])

    # Время могло «уехать»: прошло больше 2 ч до начала, Дарья закрыла день, слот заняли.
    # Повторная проверка по расписанию + атомарная вставка ячеек ниже.
    if start not in await free_starts(db, config, kind, now):
        await _edit(cb.message, content.t("book_taken"), retry_kb)
        await cb.answer()
        return

    result, booking = await db.create_booking(user.id, kind, start, now)
    if result is BookResult.TAKEN:
        await _edit(cb.message, content.t("book_taken"), retry_kb)
        await cb.answer()
        return
    if result in (BookResult.HAS_ACTIVE, BookResult.TRIAL_USED):
        key = "book_has_active" if result is BookResult.HAS_ACTIVE else "book_trial_used"
        await cb.answer(content.t(key), show_alert=True)
        return
    assert booking is not None

    await db.log_event(user.id, f"booking_{kind.value}_requested")
    slot = bk.fmt_slot(start, kind, config.client_tz)
    await _edit(
        cb.message,
        content.t("book_requested", kind=bui.kind_title(content, kind), slot=slot),
        bui.subscribe_keyboard(content, config.channel_url),
    )
    await cb.answer()

    client = bui.client_link(user.id, user.username, user.first_name)
    text = bui.owner_booking_text(
        booking, client, config.owner_tz, config.client_tz, "🆕 Новая заявка на созвон"
    )
    sent = await _send(bot, config.booking_owner_id, text, bui.owner_decision_keyboard(booking))
    if sent is not None:
        await db.set_owner_msg(booking.id, sent.message_id)
    else:
        log.warning("Booking %s: owner %s was not notified", booking.id, config.booking_owner_id)


@router.callback_query(bui.BookCancelCb.filter())
async def on_client_cancel(
    cb: CallbackQuery, callback_data: bui.BookCancelCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    now = bk.utcnow()
    b = await db.get_booking(callback_data.id)
    if b is None or b.user_id != cb.from_user.id or b.status not in ACTIVE:
        await cb.answer("Эта запись уже неактуальна")
        text, kb = await _menu(db, content, config, cb.from_user.id)
        await _edit(cb.message, text, kb)
        return
    if not bk.can_client_cancel(b.start, now):
        await cb.answer(content.t("book_cancel_too_late"), show_alert=True)
        return
    if not callback_data.sure:
        text, kb = bui.cancel_ask_screen(content, b, config.client_tz)
        await _edit(cb.message, text, kb)
        await cb.answer()
        return

    ok = await db.transition(
        b.id, ACTIVE, Status.CANCELLED, now, start_after=now + bk.CANCEL_BEFORE
    )
    if not ok:
        await cb.answer(content.t("book_cancel_too_late"), show_alert=True)
        return
    await db.log_event(cb.from_user.id, f"booking_{b.kind.value}_cancelled")
    await _edit(cb.message, content.t("book_cancelled"), None)
    await cb.answer()
    b.status = Status.CANCELLED
    await _update_owner_message(bot, db, config, b)
    await _send(bot, config.booking_owner_id,
                await _owner_text(db, config, b, "✖ Клиент отменил запись"))


# --- Дарья: заявки ---

async def _owner_list(db: Database, config: Config) -> bui.Screen:
    now = bk.utcnow()
    items = [(b, await _client_link(db, b.user_id)) for b in await db.upcoming_bookings(now)]
    return bui.owner_list_screen(items, config.owner_tz, config.client_tz)


@router.message(Command("bookings"))
async def cmd_bookings(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    if not is_owner(message.from_user, config):
        return
    await state.clear()
    text, kb = await _owner_list(db, config)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(bui.OwnerListCb.filter())
async def on_owner_list(cb: CallbackQuery, db: Database, config: Config) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    text, kb = await _owner_list(db, config)
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.OwnerDecisionCb.filter())
async def on_owner_decision(
    cb: CallbackQuery, callback_data: bui.OwnerDecisionCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    now = bk.utcnow()
    to = Status.CONFIRMED if callback_data.ok else Status.DECLINED
    # Подтвердить можно, только пока бронь не истекла.
    changed = await db.transition(callback_data.id, (Status.PENDING,), to, now, hold_active=True)
    b = await db.get_booking(callback_data.id)
    if b is None:
        await cb.answer("Запись не найдена")
        return

    if callback_data.from_list:
        text, kb = await _owner_list(db, config)
        await _edit(cb.message, text, kb)
    else:
        await _edit(cb.message, await _owner_text(db, config, b, "Заявка на созвон"), None)
    if not changed:
        await cb.answer(f"Уже нельзя: {bui.STATUS_LINE[b.status]}", show_alert=True)
        return
    await cb.answer("Готово")
    if callback_data.from_list:
        await _update_owner_message(bot, db, config, b)

    slot = bk.fmt_slot(b.start, b.kind, config.client_tz)
    if to is Status.CONFIRMED:
        await db.log_event(b.user_id, f"booking_{b.kind.value}_confirmed")
        await _send(bot, b.user_id, content.t(
            "book_confirmed", kind=bui.kind_title(content, b.kind), slot=slot
        ), bui.subscribe_keyboard(content, config.channel_url))
    else:
        await db.log_event(b.user_id, f"booking_{b.kind.value}_declined")
        await _send(bot, b.user_id, content.t("book_declined", slot=slot),
                    bui.rebook_keyboard(content, b.kind))


@router.callback_query(bui.OwnerCancelCb.filter())
async def on_owner_cancel(
    cb: CallbackQuery, callback_data: bui.OwnerCancelCb,
    db: Database, content: Content, config: Config, bot: Bot,
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    b = await db.get_booking(callback_data.id)
    if b is None or b.status not in ACTIVE:
        await cb.answer("Эта запись уже неактуальна")
        text, kb = await _owner_list(db, config)
        await _edit(cb.message, text, kb)
        return
    if not callback_data.sure:
        text, kb = bui.owner_cancel_ask(b, config.owner_tz)
        await _edit(cb.message, text, kb)
        await cb.answer()
        return
    now = bk.utcnow()
    if await db.transition(b.id, ACTIVE, Status.CANCELLED_BY_OWNER, now):
        await db.log_event(b.user_id, f"booking_{b.kind.value}_cancelled_by_owner")
        b.status = Status.CANCELLED_BY_OWNER
        await _update_owner_message(bot, db, config, b)
        slot = bk.fmt_slot(b.start, b.kind, config.client_tz)
        await _send(bot, b.user_id, content.t("book_cancelled_by_owner", slot=slot),
                    bui.rebook_keyboard(content, b.kind))
        await cb.answer("Отменено")
    else:
        await cb.answer("Эта запись уже неактуальна")
    text, kb = await _owner_list(db, config)
    await _edit(cb.message, text, kb)


# --- Дарья: расписание ---

async def _schedule(db: Database, config: Config) -> bui.Screen:
    schedule = await db.load_schedule(config.owner_tz)
    return bui.schedule_screen(schedule, config.client_tz, bk.utcnow())


@router.message(Command("schedule"))
async def cmd_schedule(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    if not is_owner(message.from_user, config):
        return
    await state.clear()
    text, kb = await _schedule(db, config)
    await message.answer(text, reply_markup=kb)


@router.message(Command("cancel"), StateFilter(SchedInput))
async def cmd_cancel_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, ничего не меняю. Расписание — /schedule")


@router.callback_query(bui.SchedCb.filter())
async def on_schedule(
    cb: CallbackQuery, db: Database, config: Config, state: FSMContext
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    await state.clear()
    text, kb = await _schedule(db, config)
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.SchedWeekdayCb.filter())
async def on_schedule_weekday(
    cb: CallbackQuery, callback_data: bui.SchedWeekdayCb, config: Config, state: FSMContext
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    if not 0 <= callback_data.wd <= 6:
        await cb.answer()
        return
    await state.set_state(SchedInput.weekday)
    await state.update_data(wd=callback_data.wd)
    name = bk.WEEKDAYS_FULL[callback_data.wd]
    await cb.message.answer(f"<b>{name}</b>\n\n{bui.HOURS_HINT}")
    await cb.answer()


@router.callback_query(bui.SchedDatesCb.filter())
async def on_schedule_dates(
    cb: CallbackQuery, db: Database, config: Config, state: FSMContext
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    await state.clear()
    schedule = await db.load_schedule(config.owner_tz)
    text, kb = bui.schedule_dates_screen(schedule, bk.utcnow())
    await _edit(cb.message, text, kb)
    await cb.answer()


async def _bookings_on(db: Database, config: Config, day_key: str) -> int:
    day = bui.parse_day(day_key)
    return sum(
        1 for b in await db.upcoming_bookings(bk.utcnow())
        if b.start.astimezone(config.owner_tz).date() == day
    )


async def _date_screen(db: Database, config: Config, day_key: str) -> bui.Screen:
    schedule = await db.load_schedule(config.owner_tz)
    return bui.schedule_date_screen(
        schedule, bui.parse_day(day_key), await _bookings_on(db, config, day_key)
    )


@router.callback_query(bui.SchedDateCb.filter())
async def on_schedule_date(
    cb: CallbackQuery, callback_data: bui.SchedDateCb, db: Database, config: Config
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    text, kb = await _date_screen(db, config, callback_data.day)
    await _edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(bui.SchedDateActCb.filter())
async def on_schedule_date_act(
    cb: CallbackQuery, callback_data: bui.SchedDateActCb,
    db: Database, config: Config, state: FSMContext,
) -> None:
    if not is_owner(cb.from_user, config) or not isinstance(cb.message, Message):
        await cb.answer()
        return
    day = bui.parse_day(callback_data.day)
    if callback_data.act == "custom":
        await state.set_state(SchedInput.date)
        await state.update_data(day=callback_data.day)
        await cb.message.answer(f"<b>{bk.fmt_day(day).capitalize()}</b>\n\n{bui.HOURS_HINT}")
        await cb.answer()
        return
    if callback_data.act == "off":
        await db.set_date(day, [])
    elif callback_data.act == "reset":
        await db.set_date(day, None)
    text, kb = await _date_screen(db, config, callback_data.day)
    await _edit(cb.message, text, kb)
    await cb.answer("Сохранено")


@router.message(StateFilter(SchedInput), F.text, ~F.text.startswith("/"))
async def on_hours_input(
    message: Message, db: Database, config: Config, state: FSMContext
) -> None:
    if not is_owner(message.from_user, config):
        await state.clear()
        return
    try:
        intervals = bk.parse_intervals(message.text or "")
    except bk.ScheduleError as e:
        await message.answer(f"{e}\n\nПопробуй ещё раз или /cancel")
        return
    data = await state.get_data()
    current = await state.get_state()
    await state.clear()
    if current == SchedInput.weekday.state:
        await db.set_weekday(int(data["wd"]), intervals)
        text, kb = await _schedule(db, config)
    else:
        await db.set_date(bui.parse_day(data["day"]), intervals)
        text, kb = await _date_screen(db, config, data["day"])
    await message.answer("Сохранено ✅\n\n" + text, reply_markup=kb)


# --- фоновые задачи: истечение брони и напоминания ---

async def process_bookings(bot: Bot, db: Database, content: Content, config: Config) -> None:
    now = bk.utcnow()

    for b in await db.holds_to_expire(now):
        if not await db.transition(b.id, (Status.PENDING,), Status.EXPIRED, now):
            continue
        b.status = Status.EXPIRED
        await db.log_event(b.user_id, f"booking_{b.kind.value}_expired")
        slot = bk.fmt_slot(b.start, b.kind, config.client_tz)
        await _send(bot, b.user_id, content.t("book_expired", slot=slot),
                    bui.rebook_keyboard(content, b.kind))
        await _update_owner_message(bot, db, config, b)
        await _send(bot, config.booking_owner_id,
                    await _owner_text(db, config, b, "⌛ Заявка снята — не подтверждена вовремя"))

    # Напоминание «завтра» — только если встречу подтвердили больше чем за сутки,
    # иначе клиент получит его сразу после подтверждения, что странно.
    for b in await db.reminders_due("reminded_day_at", timedelta(hours=24), now):
        await db.mark_booking(b.id, "reminded_day_at", now)
        decided = b.decided_at or now
        if b.start - decided >= timedelta(hours=24) and b.start - now > timedelta(hours=2):
            await _send(bot, b.user_id, content.t(
                "book_remind_day", kind=bui.kind_title(content, b.kind),
                slot=bk.fmt_slot(b.start, b.kind, config.client_tz),
            ))

    for b in await db.reminders_due("reminded_hour_at", timedelta(hours=1), now):
        await db.mark_booking(b.id, "reminded_hour_at", now)
        decided = b.decided_at or now
        if b.start - decided >= timedelta(hours=1):
            await _send(bot, b.user_id, content.t(
                "book_remind_hour", slot=bk.fmt_slot(b.start, b.kind, config.client_tz)
            ))


async def booking_loop(bot: Bot, db: Database, content: Content, config: Config) -> None:
    log.info("Booking enabled, owner %s", config.booking_owner_id)
    while True:
        try:
            await process_bookings(bot, db, content, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Booking pass failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)
