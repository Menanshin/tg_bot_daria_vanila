"""Запись на созвон: правила, расписание и расчёт свободных слотов.

Чистая логика без Telegram и базы — всё, что зависит от времени, принимает `now`.

Как устроена защита от пересечений: время режется на ячейки по 15 минут.
Запись занимает ячейки [начало, конец + GAP) — то есть сам звонок и час
«тишины» после него. В базе у ячейки первичный ключ, поэтому две записи
физически не могут занять одну ячейку: вторая вставка падает с IntegrityError.
Буфера только «после» достаточно: если B стоит раньше A, то хвост B
упрётся в первую ячейку A, и между ними всё равно останется ≥ GAP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from zoneinfo import ZoneInfo

# --- правила (меняются здесь) ---

HORIZON = timedelta(days=30)        # насколько вперёд можно записаться
MIN_LEAD = timedelta(hours=2)       # минимум до начала встречи в момент записи
CANCEL_BEFORE = timedelta(minutes=30)  # клиент может отменить не позже чем за …
GAP = timedelta(hours=1)            # минимальное окно между записями
HOLD = timedelta(hours=24)          # сколько слот ждёт подтверждения Дарьи
STEP_MINUTES = 30                   # шаг начала слотов: 10:00, 10:30, 11:00 …
CELL = timedelta(minutes=15)        # размер ячейки занятости


class Kind(StrEnum):
    TRIAL = "trial"
    FULL = "full"

    @property
    def minutes(self) -> int:
        return 15 if self is Kind.TRIAL else 60


class Status(StrEnum):
    PENDING = "pending"              # ждёт подтверждения Дарьи, слот занят
    CONFIRMED = "confirmed"
    DECLINED = "declined"            # Дарья отклонила
    EXPIRED = "expired"              # Дарья не ответила за HOLD
    CANCELLED = "cancelled"          # отменил клиент
    CANCELLED_BY_OWNER = "cancelled_by_owner"


ACTIVE = (Status.PENDING, Status.CONFIRMED)


def utcnow() -> datetime:
    """Единая точка «сейчас» — тесты подменяют её."""
    return datetime.now(UTC)


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def from_ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


# --- рабочие часы ---

Interval = tuple[int, int]  # минуты от полуночи по времени Дарьи: (600, 1140) = 10:00–19:00

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")
DAY_OFF_WORDS = {"выходной", "нет", "-", "—", "0"}


class ScheduleError(ValueError):
    pass


def _parse_time(raw: str) -> int:
    m = _TIME_RE.fullmatch(raw.strip())
    if not m:
        raise ScheduleError(f"Не понимаю время «{raw.strip()}». Пример: 10:00")
    h, mins = int(m.group(1)), int(m.group(2) or 0)
    if h == 24 and mins == 0:
        return 24 * 60
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        raise ScheduleError(f"Нет такого времени: «{raw.strip()}»")
    return h * 60 + mins


def parse_intervals(raw: str) -> list[Interval]:
    """«10:00-14:00, 16-20» → [(600, 840), (960, 1200)]; «выходной» → []."""
    text = raw.strip().lower()
    if text in DAY_OFF_WORDS:
        return []
    result: list[Interval] = []
    for part in re.split(r"[,;\n]+", text):
        part = part.strip()
        if not part:
            continue
        bounds = re.split(r"\s*[-–—]\s*", part)
        if len(bounds) != 2:
            raise ScheduleError(f"Не понимаю «{part}». Пример: 10:00-19:00")
        start, end = _parse_time(bounds[0]), _parse_time(bounds[1])
        if end <= start:
            raise ScheduleError(f"«{part}»: конец должен быть позже начала")
        result.append((start, end))
    if not result:
        raise ScheduleError("Пусто. Пример: 10:00-19:00 или «выходной»")
    result.sort()
    for (_, prev_end), (next_start, _) in pairwise(result):
        if next_start < prev_end:
            raise ScheduleError("Промежутки пересекаются")
    return result


def format_intervals(intervals: list[Interval]) -> str:
    if not intervals:
        return "выходной"
    return ", ".join(f"{_hm(a)}–{_hm(b)}" for a, b in intervals)


def dump_intervals(intervals: list[Interval]) -> str:
    """Для хранения в базе: «600-840,960-1200», пустая строка — выходной."""
    return ",".join(f"{a}-{b}" for a, b in intervals)


def load_intervals(raw: str) -> list[Interval]:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        a, b = part.split("-")
        out.append((int(a), int(b)))
    return out


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class Schedule:
    """Недельный шаблон + особые даты. Всё по времени Дарьи."""

    week: dict[int, list[Interval]]          # 0 = понедельник
    dates: dict[date, list[Interval]]        # переопределение на конкретный день
    tz: ZoneInfo

    def intervals_for(self, day: date) -> list[Interval]:
        if day in self.dates:
            return self.dates[day]
        return self.week.get(day.weekday(), [])


# --- слоты ---

def cells_for(start: datetime, kind: Kind) -> list[int]:
    """Ячейки, которые займёт запись: звонок + окно GAP после него."""
    end = start + timedelta(minutes=kind.minutes) + GAP
    cells, t = [], start
    while t < end:
        cells.append(ts(t))
        t += CELL
    return cells


def candidate_starts(schedule: Schedule, kind: Kind, now: datetime) -> list[datetime]:
    """Все начала в рабочие часы в пределах [now + MIN_LEAD, now + HORIZON], без учёта занятости."""
    earliest, latest = now + MIN_LEAD, now + HORIZON
    first_day = earliest.astimezone(schedule.tz).date() - timedelta(days=1)
    last_day = latest.astimezone(schedule.tz).date()
    out: list[datetime] = []
    day = first_day
    while day <= last_day:
        for a, b in schedule.intervals_for(day):
            t = -(-a // STEP_MINUTES) * STEP_MINUTES  # округление вверх до шага
            while t + kind.minutes <= b:
                local = datetime(day.year, day.month, day.day, t // 60, t % 60,
                                 tzinfo=schedule.tz)
                start = local.astimezone(UTC)
                if earliest <= start <= latest:
                    out.append(start)
                t += STEP_MINUTES
        day += timedelta(days=1)
    return sorted(set(out))


def free_starts(
    schedule: Schedule, kind: Kind, now: datetime, busy: set[int]
) -> list[datetime]:
    return [
        s for s in candidate_starts(schedule, kind, now)
        if not any(c in busy for c in cells_for(s, kind))
    ]


def group_by_day(starts: list[datetime], tz: ZoneInfo) -> dict[date, list[datetime]]:
    out: dict[date, list[datetime]] = {}
    for s in starts:
        out.setdefault(s.astimezone(tz).date(), []).append(s)
    return out


def hold_until(now: datetime, start: datetime) -> datetime:
    """Слот ждёт Дарью сутки, но не дольше начала самой встречи."""
    return min(now + HOLD, start)


def can_client_cancel(start: datetime, now: datetime) -> bool:
    return now <= start - CANCEL_BEFORE


# --- форматирование дат по-русски ---

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAYS_FULL = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def fmt_day(day: date) -> str:
    """«вт, 15 октября»"""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def fmt_short_day(day: date) -> str:
    """«Вт 15.10» — для кнопок."""
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day:02d}.{day.month:02d}"


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def fmt_slot(start: datetime, kind: Kind, tz: ZoneInfo) -> str:
    """«вт, 15 октября, 18:00–19:00»"""
    local = start.astimezone(tz)
    end = start + timedelta(minutes=kind.minutes)
    return f"{fmt_day(local.date())}, {fmt_time(start, tz)}–{fmt_time(end, tz)}"
