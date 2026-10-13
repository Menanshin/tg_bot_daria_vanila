"""Экраны и клавиатуры записи на созвон: для клиента (по МСК) и для Даши (по её времени)."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import booking as bk
from bot.booking import Kind, Status
from bot.content import Content
from bot.db import Booking

DAYS_PER_PAGE = 12

Screen = tuple[str, InlineKeyboardMarkup]


# --- callback data ---

class BookMenuCb(CallbackData, prefix="bk"):
    edit: bool = False  # True — перерисовать текущее сообщение, False — прислать новое


class BookKindCb(CallbackData, prefix="bkk"):
    kind: Kind
    page: int = 0


class BookDayCb(CallbackData, prefix="bkd"):
    kind: Kind
    day: str  # YYYYMMDD по времени клиента


class BookTimeCb(CallbackData, prefix="bkt"):
    kind: Kind
    at: int  # unix-время начала


class BookConfirmCb(CallbackData, prefix="bkc"):
    kind: Kind
    at: int


class BookCancelCb(CallbackData, prefix="bkx"):
    id: int
    sure: bool = False


class OwnerDecisionCb(CallbackData, prefix="bko"):
    id: int
    ok: bool
    from_list: bool = False


class OwnerCancelCb(CallbackData, prefix="bkoc"):
    id: int
    sure: bool = False


class OwnerListCb(CallbackData, prefix="bkol"):
    pass


class SchedCb(CallbackData, prefix="sch"):
    pass


class SchedWeekdayCb(CallbackData, prefix="schw"):
    wd: int


class SchedDatesCb(CallbackData, prefix="schd"):
    pass


class SchedDateCb(CallbackData, prefix="schdd"):
    day: str  # YYYYMMDD по времени Даши


class SchedDateActCb(CallbackData, prefix="schda"):
    day: str
    act: str  # off | custom | reset


def day_key(day: date) -> str:
    return day.strftime("%Y%m%d")


def parse_day(key: str) -> date:
    return datetime.strptime(key, "%Y%m%d").date()


def button(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def kind_title(content: Content, kind: Kind) -> str:
    return content.t("book_kind_trial" if kind is Kind.TRIAL else "book_kind_full")


def book_button(content: Content) -> InlineKeyboardButton:
    return button(content.t("book_button"), BookMenuCb())


def subscribe_keyboard(content: Content, channel_url: str) -> InlineKeyboardMarkup:
    """После заявки и после подтверждения — подписка на канал Даши."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=content.t("subscribe_button"), url=channel_url)
    ]])


def rebook_keyboard(content: Content, kind: Kind) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        button("📅 Выбрать время", BookKindCb(kind=kind))
    ]])


# --- клиент ---

def menu_screen(
    content: Content, tz: ZoneInfo, bookings: list[Booking], trial_used: bool
) -> Screen:
    parts = [content.t("book_menu")]
    rows: list[list[InlineKeyboardButton]] = []
    if bookings:
        lines = [content.t("book_my_bookings")]
        for b in bookings:
            status = content.t(
                "book_status_pending" if b.status is Status.PENDING else "book_status_confirmed"
            )
            lines.append(
                f"• {kind_title(content, b.kind)}\n  🗓 {bk.fmt_slot(b.start, b.kind, tz)} МСК"
                f" — {status}"
            )
            local = b.start.astimezone(tz)
            rows.append([button(
                f"✖ Отменить {bk.fmt_short_day(local.date())} {local:%H:%M}",
                BookCancelCb(id=b.id),
            )])
        parts.append("\n".join(lines))
    active_kinds = {b.kind for b in bookings}
    kind_rows = []
    if not trial_used:
        title = "🌱 " + kind_title(content, Kind.TRIAL)
        kind_rows.append([button(title, BookKindCb(kind=Kind.TRIAL))])
    if Kind.FULL not in active_kinds:
        title = "💜 " + kind_title(content, Kind.FULL)
        kind_rows.append([button(title, BookKindCb(kind=Kind.FULL))])
    return "\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=kind_rows + rows)


def days_screen(
    content: Content, kind: Kind, days: dict[date, list[datetime]], page: int
) -> Screen:
    back = [button("← Назад", BookMenuCb(edit=True))]
    if not days:
        return content.t("book_no_days"), InlineKeyboardMarkup(inline_keyboard=[back])
    ordered = sorted(days)
    pages = (len(ordered) + DAYS_PER_PAGE - 1) // DAYS_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = ordered[page * DAYS_PER_PAGE:(page + 1) * DAYS_PER_PAGE]
    buttons = [
        button(f"{bk.fmt_short_day(d)} · {len(days[d])}", BookDayCb(kind=kind, day=day_key(d)))
        for d in chunk
    ]
    rows = _rows(buttons, 3)
    nav = []
    if page > 0:
        nav.append(button("◀ Раньше", BookKindCb(kind=kind, page=page - 1)))
    if page < pages - 1:
        nav.append(button("Позже ▶", BookKindCb(kind=kind, page=page + 1)))
    if nav:
        rows.append(nav)
    rows.append(back)
    text = content.t("book_pick_day", kind=kind_title(content, kind))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def page_of(days: dict[date, list[datetime]], day: date) -> int:
    ordered = sorted(days)
    return ordered.index(day) // DAYS_PER_PAGE if day in ordered else 0


def times_screen(
    content: Content, kind: Kind, day: date, starts: list[datetime], tz: ZoneInfo, page: int
) -> Screen:
    back = [button("← Другой день", BookKindCb(kind=kind, page=page))]
    if not starts:
        return content.t("book_no_times"), InlineKeyboardMarkup(inline_keyboard=[back])
    buttons = [
        button(bk.fmt_time(s, tz), BookTimeCb(kind=kind, at=bk.ts(s))) for s in starts
    ]
    text = content.t(
        "book_pick_time", kind=kind_title(content, kind), day=bk.fmt_day(day).capitalize()
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 4), back])


def confirm_screen(content: Content, kind: Kind, start: datetime, tz: ZoneInfo) -> Screen:
    day = start.astimezone(tz).date()
    text = content.t(
        "book_confirm", kind=kind_title(content, kind), slot=bk.fmt_slot(start, kind, tz)
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [button(content.t("book_confirm_button"), BookConfirmCb(kind=kind, at=bk.ts(start)))],
        [button("← Другое время", BookDayCb(kind=kind, day=day_key(day)))],
    ])
    return text, kb


def cancel_ask_screen(content: Content, b: Booking, tz: ZoneInfo) -> Screen:
    text = content.t(
        "book_cancel_ask", kind=kind_title(content, b.kind), slot=bk.fmt_slot(b.start, b.kind, tz)
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        button(content.t("book_cancel_yes"), BookCancelCb(id=b.id, sure=True)),
        button(content.t("book_cancel_no"), BookMenuCb(edit=True)),
    ]])
    return text, kb


# --- Даша ---

KIND_SHORT = {Kind.TRIAL: "Пробная · 15 мин", Kind.FULL: "Консультация · 60 мин"}
STATUS_LINE = {
    Status.PENDING: "⏳ ждёт подтверждения",
    Status.CONFIRMED: "✅ подтверждена",
    Status.DECLINED: "❌ отклонена",
    Status.EXPIRED: "⌛ не подтверждена вовремя — слот освобождён",
    Status.CANCELLED: "✖ клиент отменил",
    Status.CANCELLED_BY_OWNER: "✖ ты отменила",
}


def client_link(user_id: int, username: str | None, first_name: str | None = None) -> str:
    name = html.escape(first_name or (f"@{username}" if username else "клиент"))
    link = f'<a href="tg://user?id={user_id}">{name}</a>'
    if username and first_name:
        link += f" (@{html.escape(username)})"
    return link


def _dt(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return f"{bk.fmt_day(local.date())}, {local:%H:%M}"


def owner_booking_text(
    b: Booking, client: str, owner_tz: ZoneInfo, client_tz: ZoneInfo, header: str
) -> str:
    lines = [
        f"<b>{header}</b>",
        KIND_SHORT[b.kind],
        f"🗓 {bk.fmt_slot(b.start, b.kind, owner_tz)} (твоё время)",
        f"    = {bk.fmt_time(b.start, client_tz)} МСК",
        f"👤 {client}",
    ]
    if b.status is Status.PENDING:
        lines.append(f"\nСлот держится до {_dt(b.hold_until, owner_tz)}, потом освободится.")
    else:
        lines.append(f"\n{STATUS_LINE[b.status]}")
    return "\n".join(lines)


def owner_decision_keyboard(b: Booking, from_list: bool = False) -> InlineKeyboardMarkup | None:
    if b.status is not Status.PENDING:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        button("✅ Подтвердить", OwnerDecisionCb(id=b.id, ok=True, from_list=from_list)),
        button("❌ Отклонить", OwnerDecisionCb(id=b.id, ok=False, from_list=from_list)),
    ]])


def owner_list_screen(
    items: list[tuple[Booking, str]], owner_tz: ZoneInfo, client_tz: ZoneInfo
) -> Screen:
    if not items:
        return "Предстоящих записей нет.", InlineKeyboardMarkup(inline_keyboard=[
            [button("🔄 Обновить", OwnerListCb())]
        ])
    lines = ["<b>📋 Предстоящие записи</b> (твоё время)"]
    rows: list[list[InlineKeyboardButton]] = []
    for b, client in items:
        local = b.start.astimezone(owner_tz)
        short = f"{local:%d.%m %H:%M}"
        lines.append(
            f"\n{STATUS_LINE[b.status]}\n{KIND_SHORT[b.kind]}\n"
            f"🗓 {bk.fmt_slot(b.start, b.kind, owner_tz)} (= {bk.fmt_time(b.start, client_tz)} МСК)"
            f"\n👤 {client}"
        )
        if b.status is Status.PENDING:
            rows.append([
                button(f"✅ {short}", OwnerDecisionCb(id=b.id, ok=True, from_list=True)),
                button("❌ Отклонить", OwnerDecisionCb(id=b.id, ok=False, from_list=True)),
            ])
        else:
            rows.append([button(f"✖ Отменить {short}", OwnerCancelCb(id=b.id))])
    rows.append([button("🔄 Обновить", OwnerListCb())])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def owner_cancel_ask(b: Booking, owner_tz: ZoneInfo) -> Screen:
    text = (
        f"Отменить запись {bk.fmt_slot(b.start, b.kind, owner_tz)}?\n"
        "Клиент получит сообщение, что встреча отменена."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        button("Да, отменить", OwnerCancelCb(id=b.id, sure=True)),
        button("Нет", OwnerListCb()),
    ]])
    return text, kb


def _offset_hint(owner_tz: ZoneInfo, client_tz: ZoneInfo, now: datetime) -> str:
    diff = now.astimezone(owner_tz).utcoffset() - now.astimezone(client_tz).utcoffset()  # type: ignore[operator]
    hours = diff.total_seconds() / 3600
    if hours == 0:
        return "Клиенты видят то же время."
    sign = "+" if hours > 0 else "−"
    return f"Клиенты видят слоты по Москве: твоё время = МСК {sign} {abs(hours):g} ч."


def schedule_screen(schedule: bk.Schedule, client_tz: ZoneInfo, now: datetime) -> Screen:
    lines = ["<b>🗓 Расписание</b> (твоё время)"]
    for wd in range(7):
        lines.append(f"{bk.WEEKDAYS[wd].capitalize()}: "
                     f"{bk.format_intervals(schedule.week.get(wd, []))}")
    today = now.astimezone(schedule.tz).date()
    special = sorted(d for d in schedule.dates if d >= today)
    if special:
        lines.append("\n<b>Особые даты</b>")
        lines += [f"• {bk.fmt_day(d)} — {bk.format_intervals(schedule.dates[d])}" for d in special]
    lines.append("")
    lines.append(_offset_hint(schedule.tz, client_tz, now))
    lines.append("Нажми день недели, чтобы поменять часы. Отпуск или другие часы на "
                 "конкретную дату — «Особая дата».")
    buttons = [button(bk.WEEKDAYS[wd].capitalize(), SchedWeekdayCb(wd=wd)) for wd in range(7)]
    rows = [buttons[:4], buttons[4:], [button("📅 Особая дата", SchedDatesCb())]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_dates_screen(schedule: bk.Schedule, now: datetime) -> Screen:
    today = now.astimezone(schedule.tz).date()
    days = [today + timedelta(days=i) for i in range(bk.HORIZON.days + 1)]
    buttons = [
        button(("• " if d in schedule.dates else "") + bk.fmt_short_day(d),
             SchedDateCb(day=day_key(d)))
        for d in days
    ]
    rows = [*_rows(buttons, 4), [button("← Назад", SchedCb())]]
    text = "Выбери дату (• — уже настроена особо):"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_date_screen(schedule: bk.Schedule, day: date, bookings_that_day: int) -> Screen:
    special = day in schedule.dates
    lines = [
        f"<b>{bk.fmt_day(day).capitalize()}</b>",
        f"Сейчас: {bk.format_intervals(schedule.intervals_for(day))}"
        + (" (особая дата)" if special else " (по недельному расписанию)"),
    ]
    if bookings_that_day:
        lines.append(f"\n⚠️ На этот день уже есть записи: {bookings_that_day}. Они сохранятся — "
                     "если нужно, отмени их в /bookings.")
    key = day_key(day)
    rows = [
        [button("🚫 Выходной", SchedDateActCb(day=key, act="off"))],
        [button("✏️ Свои часы", SchedDateActCb(day=key, act="custom"))],
    ]
    if special:
        rows.append([button("↩️ Как обычно", SchedDateActCb(day=key, act="reset"))])
    rows.append([button("← К датам", SchedDatesCb())])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


HOURS_HINT = (
    "Пришли часы приёма по своему времени, например:\n"
    "<code>10:00-19:00</code>\n"
    "<code>10:00-14:00, 16:00-20:00</code>\n"
    "или слово <code>выходной</code>.\n\n"
    "Отменить — /cancel"
)
