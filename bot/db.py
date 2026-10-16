"""Хранилище на SQLite: пользователи, прохождения теста, события воронки и записи на созвон."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from bot.booking import (
    ACTIVE,
    Interval,
    Kind,
    Schedule,
    Status,
    cells_for,
    dump_intervals,
    from_ts,
    hold_until,
    load_intervals,
    ts,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL,
    blocked_at  TEXT            -- когда заблокировал бота; NULL — не блокировал или вернулся
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(user_id),
    answers        TEXT NOT NULL DEFAULT '[]',
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    anxiety        INTEGER,
    avoidance      INTEGER,
    result_type    TEXT,
    gate_shown_at  TEXT,
    unlocked_at    TEXT,
    reminded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name);

-- Запись на созвон. Время — unix-секунды UTC (удобно считать и сравнивать).
CREATE TABLE IF NOT EXISTS bookings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    starts_at          INTEGER NOT NULL,
    ends_at            INTEGER NOT NULL,
    status             TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    hold_until         INTEGER NOT NULL,
    decided_at         INTEGER,
    owner_msg_id       INTEGER,
    reminded_day_at    INTEGER,
    reminded_hour_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status, starts_at);

-- Занятые 15-минутные ячейки. PRIMARY KEY не даёт двум записям занять одно время.
CREATE TABLE IF NOT EXISTS booking_cells (
    cell        INTEGER PRIMARY KEY,
    booking_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cells_booking ON booking_cells(booking_id);

-- Рабочие часы Дарьи (по её времени). intervals: «600-1140,…», пусто — выходной.
CREATE TABLE IF NOT EXISTS schedule_week (
    weekday     INTEGER PRIMARY KEY,
    intervals   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_dates (
    day         TEXT PRIMARY KEY,
    intervals   TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class Session:
    id: int
    user_id: int
    answers: list[int]
    finished_at: str | None
    anxiety: int | None
    avoidance: int | None
    result_type: str | None
    gate_shown_at: str | None
    unlocked_at: str | None


class BookResult(StrEnum):
    OK = "ok"
    TAKEN = "taken"            # кто-то занял слот раньше
    HAS_ACTIVE = "has_active"  # уже есть будущая запись этого типа
    TRIAL_USED = "trial_used"  # пробная бывает один раз


@dataclass(slots=True)
class Booking:
    id: int
    user_id: int
    kind: Kind
    start: datetime
    status: Status
    created_at: datetime
    hold_until: datetime
    decided_at: datetime | None
    owner_msg_id: int | None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.kind.minutes)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        # Одно соединение на всех: без лока чужой commit() мог бы закоммитить
        # половину нашей транзакции. Все записи идут через write().
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Добавить колонки, которых нет в базах, созданных старой версией."""
        async with self.conn.execute("PRAGMA table_info(users)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "blocked_at" not in columns:
            await self.conn.execute("ALTER TABLE users ADD COLUMN blocked_at TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @contextlib.asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция: всё внутри либо коммитится целиком, либо откатывается."""
        async with self._lock:
            try:
                yield self.conn
            except BaseException:
                await self.conn.rollback()
                raise
            await self.conn.commit()

    # --- users & events ---

    async def upsert_user(self, user_id: int, username: str | None, source: str | None) -> None:
        # source запоминаем только при первом заходе — это канал привлечения.
        async with self.write() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, username, source, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
                """,
                (user_id, username, source, now_iso()),
            )

    async def set_blocked(self, user_id: int, blocked: bool) -> None:
        """Отметить, что человек заблокировал бота (или разблокировал). Повтор не плодит событий."""
        async with self.write() as conn:
            if blocked:
                cur = await conn.execute(
                    "UPDATE users SET blocked_at = ? WHERE user_id = ? AND blocked_at IS NULL",
                    (now_iso(), user_id),
                )
            else:
                cur = await conn.execute(
                    "UPDATE users SET blocked_at = NULL"
                    " WHERE user_id = ? AND blocked_at IS NOT NULL",
                    (user_id,),
                )
            if cur.rowcount:
                await conn.execute(
                    "INSERT INTO events (user_id, name, created_at) VALUES (?, ?, ?)",
                    (user_id, "blocked" if blocked else "unblocked", now_iso()),
                )

    async def get_setting(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self.write() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def log_event(self, user_id: int, name: str) -> None:
        async with self.write() as conn:
            await conn.execute(
                "INSERT INTO events (user_id, name, created_at) VALUES (?, ?, ?)",
                (user_id, name, now_iso()),
            )

    async def broadcast_recipients(self, event: str) -> list[int]:
        """Кто не заблокировал бота и ещё не получил рассылку event (повтор не дублирует)."""
        async with self.conn.execute(
            """SELECT user_id FROM users WHERE blocked_at IS NULL
               AND user_id NOT IN (SELECT user_id FROM events WHERE name = ?)
               ORDER BY created_at""",
            (event,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def username(self, user_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT username FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def has_event(self, user_id: int, name: str, since: str | None = None) -> bool:
        async with self.conn.execute(
            """SELECT 1 FROM events WHERE user_id = ? AND name = ? AND created_at >= ?
               LIMIT 1""",
            (user_id, name, since or ""),
        ) as cur:
            return await cur.fetchone() is not None

    # --- sessions ---

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> Session:
        return Session(
            id=row["id"],
            user_id=row["user_id"],
            answers=json.loads(row["answers"]),
            finished_at=row["finished_at"],
            anxiety=row["anxiety"],
            avoidance=row["avoidance"],
            result_type=row["result_type"],
            gate_shown_at=row["gate_shown_at"],
            unlocked_at=row["unlocked_at"],
        )

    async def new_session(self, user_id: int) -> Session:
        async with self.write() as conn:
            cur = await conn.execute(
                "INSERT INTO sessions (user_id, started_at) VALUES (?, ?)",
                (user_id, now_iso()),
            )
        session = await self.get_session(cur.lastrowid)  # type: ignore[arg-type]
        assert session is not None
        return session

    async def get_session(self, session_id: int) -> Session | None:
        async with self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
        return self._row_to_session(row) if row else None

    async def latest_session(self, user_id: int) -> Session | None:
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_session(row) if row else None

    async def latest_finished_session(self, user_id: int) -> Session | None:
        async with self.conn.execute(
            """SELECT * FROM sessions WHERE user_id = ? AND finished_at IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_session(row) if row else None

    async def save_answers(self, session_id: int, answers: list[int]) -> None:
        async with self.write() as conn:
            await conn.execute(
                "UPDATE sessions SET answers = ? WHERE id = ?", (json.dumps(answers), session_id)
            )

    async def finish_session(
        self, session_id: int, anxiety: int, avoidance: int, result_type: str
    ) -> None:
        async with self.write() as conn:
            await conn.execute(
                """UPDATE sessions SET finished_at = ?, anxiety = ?, avoidance = ?, result_type = ?
                   WHERE id = ?""",
                (now_iso(), anxiety, avoidance, result_type, session_id),
            )

    async def mark(self, session_id: int, column: str) -> None:
        if column not in {"gate_shown_at", "unlocked_at", "reminded_at"}:
            raise ValueError(column)
        async with self.write() as conn:
            await conn.execute(
                f"UPDATE sessions SET {column} = COALESCE({column}, ?) WHERE id = ?",  # noqa: S608
                (now_iso(), session_id),
            )

    async def sessions_to_remind(self, older_than_hours: float, limit: int = 100) -> list[Session]:
        cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).isoformat(
            timespec="seconds"
        )
        # Напоминаем только по последнему прохождению пользователя.
        async with self.conn.execute(
            """
            SELECT s.* FROM sessions s
            WHERE s.gate_shown_at IS NOT NULL AND s.gate_shown_at <= ?
              AND s.unlocked_at IS NULL AND s.reminded_at IS NULL
              AND s.id = (SELECT MAX(id) FROM sessions WHERE user_id = s.user_id)
            ORDER BY s.id LIMIT ?
            """,
            (cutoff, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_session(r) for r in rows]

    # --- stats ---

    async def stats(self) -> dict[str, object]:
        async def scalar(sql: str, *args: object) -> int:
            async with self.conn.execute(sql, args) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        async def pairs(sql: str) -> list[tuple[str, int]]:
            async with self.conn.execute(sql) as cur:
                rows = await cur.fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]

        users_with = "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE {} IS NOT NULL"
        day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
        return {
            "blocked": await scalar("SELECT COUNT(*) FROM users WHERE blocked_at IS NOT NULL"),
            "blocked_ever": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'blocked'"
            ),
            "users": await scalar("SELECT COUNT(*) FROM users"),
            "started": await scalar("SELECT COUNT(DISTINCT user_id) FROM sessions"),
            "finished": await scalar(users_with.format("finished_at")),
            "gate_shown": await scalar(users_with.format("gate_shown_at")),
            "unlocked": await scalar(users_with.format("unlocked_at")),
            "reminded": await scalar(users_with.format("reminded_at")),
            "diary_sent": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'diary_sent'"
            ),
            "about_opened": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'about_opened'"
            ),
            "reviews_opened": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'reviews_opened'"
            ),
            "course_opened": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'course_opened'"
            ),
            "course_broadcast_sent": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'course_broadcast_sent'"
            ),
            "course_reviews_opened": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'course_reviews_opened'"
            ),
            "unlocked_via_gate": await scalar(
                """SELECT COUNT(DISTINCT user_id) FROM sessions
                   WHERE gate_shown_at IS NOT NULL AND unlocked_at IS NOT NULL"""
            ),
            "users_24h": await scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", day_ago),
            "by_source": await pairs(
                """SELECT COALESCE(source, '—'), COUNT(*) FROM users
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
            ),
            "by_type": await pairs(
                """SELECT result_type, COUNT(*) FROM sessions
                   WHERE result_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""
            ),
        }

    # --- запись на созвон ---

    async def create_booking(
        self, user_id: int, kind: Kind, start: datetime, now: datetime
    ) -> tuple[BookResult, Booking | None]:
        """Атомарно: проверить лимиты, создать заявку и занять ячейки.

        Свободность слота не проверяем заранее — её гарантирует PRIMARY KEY
        ячеек: если хоть одна занята, вставка падает и всё откатывается.
        """
        start_ts = ts(start)
        end_ts = start_ts + kind.minutes * 60
        now_ts = ts(now)
        active = tuple(s.value for s in ACTIVE)
        try:
            async with self.write() as conn:
                async with conn.execute(
                    """SELECT ends_at FROM bookings
                       WHERE user_id = ? AND kind = ? AND status IN (?, ?)""",
                    (user_id, kind.value, *active),
                ) as cur:
                    rows = await cur.fetchall()
                if any(r["ends_at"] > now_ts for r in rows):
                    return BookResult.HAS_ACTIVE, None
                if kind is Kind.TRIAL and rows:
                    return BookResult.TRIAL_USED, None
                cur = await conn.execute(
                    """INSERT INTO bookings
                       (user_id, kind, starts_at, ends_at, status, created_at, hold_until)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, kind.value, start_ts, end_ts, Status.PENDING.value, now_ts,
                     ts(hold_until(now, start))),
                )
                booking_id = cur.lastrowid
                await conn.executemany(
                    "INSERT INTO booking_cells (cell, booking_id) VALUES (?, ?)",
                    [(c, booking_id) for c in cells_for(start, kind)],
                )
        except sqlite3.IntegrityError:
            return BookResult.TAKEN, None
        booking = await self.get_booking(booking_id)  # type: ignore[arg-type]
        assert booking is not None
        return BookResult.OK, booking

    async def transition(
        self, booking_id: int, from_: tuple[Status, ...], to: Status, now: datetime,
        *, hold_active: bool = False, start_after: datetime | None = None,
    ) -> bool:
        """Сменить статус, только если он сейчас один из from_. Неактивный — освобождает ячейки.

        Условие проверяется в том же UPDATE, поэтому одновременные «подтвердить»
        и «истекло» не могут оба сработать.
        """
        sql = "UPDATE bookings SET status = ?, decided_at = ? WHERE id = ? AND status IN ({})"
        sql = sql.format(",".join("?" * len(from_)))
        params: list[object] = [to.value, ts(now), booking_id, *(s.value for s in from_)]
        if hold_active:
            sql += " AND hold_until > ?"
            params.append(ts(now))
        if start_after is not None:
            sql += " AND starts_at >= ?"
            params.append(ts(start_after))
        async with self.write() as conn:
            cur = await conn.execute(sql, params)
            changed = cur.rowcount == 1
            if changed and to not in ACTIVE:
                await conn.execute("DELETE FROM booking_cells WHERE booking_id = ?", (booking_id,))
        return changed

    @staticmethod
    def _row_to_booking(row: aiosqlite.Row) -> Booking:
        return Booking(
            id=row["id"],
            user_id=row["user_id"],
            kind=Kind(row["kind"]),
            start=from_ts(row["starts_at"]),
            status=Status(row["status"]),
            created_at=from_ts(row["created_at"]),
            hold_until=from_ts(row["hold_until"]),
            decided_at=from_ts(row["decided_at"]) if row["decided_at"] else None,
            owner_msg_id=row["owner_msg_id"],
        )

    async def _bookings(self, where: str, params: tuple[object, ...]) -> list[Booking]:
        async with self.conn.execute(
            f"SELECT * FROM bookings WHERE {where} ORDER BY starts_at",  # noqa: S608
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_booking(r) for r in rows]

    async def get_booking(self, booking_id: int) -> Booking | None:
        found = await self._bookings("id = ?", (booking_id,))
        return found[0] if found else None

    async def user_bookings(self, user_id: int, now: datetime) -> list[Booking]:
        """Активные будущие записи пользователя."""
        return await self._bookings(
            "user_id = ? AND status IN (?, ?) AND ends_at > ?",
            (user_id, *(s.value for s in ACTIVE), ts(now)),
        )

    async def trial_used(self, user_id: int) -> bool:
        found = await self._bookings(
            "user_id = ? AND kind = ? AND status IN (?, ?)",
            (user_id, Kind.TRIAL.value, *(s.value for s in ACTIVE)),
        )
        return bool(found)

    async def upcoming_bookings(self, now: datetime) -> list[Booking]:
        return await self._bookings(
            "status IN (?, ?) AND ends_at > ?", (*(s.value for s in ACTIVE), ts(now))
        )

    async def holds_to_expire(self, now: datetime) -> list[Booking]:
        return await self._bookings(
            "status = ? AND hold_until <= ?", (Status.PENDING.value, ts(now))
        )

    async def reminders_due(self, column: str, before: timedelta, now: datetime) -> list[Booking]:
        if column not in {"reminded_day_at", "reminded_hour_at"}:
            raise ValueError(column)
        return await self._bookings(
            f"status = ? AND {column} IS NULL AND starts_at - ? <= ? AND starts_at > ?",
            (Status.CONFIRMED.value, int(before.total_seconds()), ts(now), ts(now)),
        )

    async def mark_booking(self, booking_id: int, column: str, now: datetime) -> None:
        if column not in {"reminded_day_at", "reminded_hour_at"}:
            raise ValueError(column)
        async with self.write() as conn:
            await conn.execute(
                f"UPDATE bookings SET {column} = ? WHERE id = ?",  # noqa: S608
                (ts(now), booking_id),
            )

    async def set_owner_msg(self, booking_id: int, message_id: int) -> None:
        async with self.write() as conn:
            await conn.execute(
                "UPDATE bookings SET owner_msg_id = ? WHERE id = ?", (message_id, booking_id)
            )

    async def busy_cells(self, start: datetime, end: datetime) -> set[int]:
        async with self.conn.execute(
            "SELECT cell FROM booking_cells WHERE cell >= ? AND cell < ?",
            (ts(start), ts(end)),
        ) as cur:
            rows = await cur.fetchall()
        return {int(r[0]) for r in rows}

    # --- расписание Дарьи ---

    async def load_schedule(self, tz: ZoneInfo) -> Schedule:
        async with self.conn.execute("SELECT weekday, intervals FROM schedule_week") as cur:
            week = {int(r[0]): load_intervals(r[1]) for r in await cur.fetchall()}
        async with self.conn.execute("SELECT day, intervals FROM schedule_dates") as cur:
            dates = {date.fromisoformat(r[0]): load_intervals(r[1]) for r in await cur.fetchall()}
        return Schedule(week=week, dates=dates, tz=tz)

    async def set_weekday(self, weekday: int, intervals: list[Interval]) -> None:
        async with self.write() as conn:
            await conn.execute(
                """INSERT INTO schedule_week (weekday, intervals) VALUES (?, ?)
                   ON CONFLICT(weekday) DO UPDATE SET intervals = excluded.intervals""",
                (weekday, dump_intervals(intervals)),
            )

    async def set_date(self, day: date, intervals: list[Interval] | None) -> None:
        """None — убрать особую дату (день снова по недельному шаблону)."""
        async with self.write() as conn:
            if intervals is None:
                await conn.execute("DELETE FROM schedule_dates WHERE day = ?", (day.isoformat(),))
            else:
                await conn.execute(
                    """INSERT INTO schedule_dates (day, intervals) VALUES (?, ?)
                       ON CONFLICT(day) DO UPDATE SET intervals = excluded.intervals""",
                    (day.isoformat(), dump_intervals(intervals)),
                )
