"""Хранилище на SQLite: пользователи, прохождения теста и события воронки."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL
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


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    # --- users & events ---

    async def upsert_user(self, user_id: int, username: str | None, source: str | None) -> None:
        # source запоминаем только при первом заходе — это канал привлечения.
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, source, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username, source, now_iso()),
        )
        await self.conn.commit()

    async def log_event(self, user_id: int, name: str) -> None:
        await self.conn.execute(
            "INSERT INTO events (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, now_iso()),
        )
        await self.conn.commit()

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
        cur = await self.conn.execute(
            "INSERT INTO sessions (user_id, started_at) VALUES (?, ?)",
            (user_id, now_iso()),
        )
        await self.conn.commit()
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
        await self.conn.execute(
            "UPDATE sessions SET answers = ? WHERE id = ?", (json.dumps(answers), session_id)
        )
        await self.conn.commit()

    async def finish_session(
        self, session_id: int, anxiety: int, avoidance: int, result_type: str
    ) -> None:
        await self.conn.execute(
            """UPDATE sessions SET finished_at = ?, anxiety = ?, avoidance = ?, result_type = ?
               WHERE id = ?""",
            (now_iso(), anxiety, avoidance, result_type, session_id),
        )
        await self.conn.commit()

    async def mark(self, session_id: int, column: str) -> None:
        if column not in {"gate_shown_at", "unlocked_at", "reminded_at"}:
            raise ValueError(column)
        await self.conn.execute(
            f"UPDATE sessions SET {column} = COALESCE({column}, ?) WHERE id = ?",  # noqa: S608
            (now_iso(), session_id),
        )
        await self.conn.commit()

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
        return {
            "users": await scalar("SELECT COUNT(*) FROM users"),
            "started": await scalar("SELECT COUNT(DISTINCT user_id) FROM sessions"),
            "finished": await scalar(users_with.format("finished_at")),
            "gate_shown": await scalar(users_with.format("gate_shown_at")),
            "unlocked": await scalar(users_with.format("unlocked_at")),
            "reminded": await scalar(users_with.format("reminded_at")),
            "diary_sent": await scalar(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE name = 'diary_sent'"
            ),
            "unlocked_via_gate": await scalar(
                """SELECT COUNT(DISTINCT user_id) FROM sessions
                   WHERE gate_shown_at IS NOT NULL AND unlocked_at IS NOT NULL"""
            ),
            "users_24h": await scalar(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?",
                (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds"),
            ),
            "by_source": await pairs(
                """SELECT COALESCE(source, '—'), COUNT(*) FROM users
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
            ),
            "by_type": await pairs(
                """SELECT result_type, COUNT(*) FROM sessions
                   WHERE result_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""
            ),
        }
