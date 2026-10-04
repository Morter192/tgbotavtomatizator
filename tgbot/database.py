"""SQLite для прив’язки особистого чату та майбутніх налаштувань."""

import asyncio
import sqlite3
import json
from dataclasses import asdict
from contextlib import closing
from pathlib import Path

from tgbot.schedule import Lesson


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS owner_settings (
                    telegram_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (reminders_enabled IN (0, 1)),
                    reminder_minutes INTEGER NOT NULL DEFAULT 10
                        CHECK (reminder_minutes > 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS schedule_info (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    source TEXT NOT NULL,
                    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    subgroup INTEGER,
                    payload TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS lessons_profile ON lessons(group_name, subgroup)")
            info_columns = {row[1] for row in connection.execute("PRAGMA table_info(schedule_info)")}
            for name in ("source_path", "source_hash"):
                if name not in info_columns:
                    connection.execute(f"ALTER TABLE schedule_info ADD COLUMN {name} TEXT")
            connection.execute("CREATE TABLE IF NOT EXISTS source_status (id INTEGER PRIMARY KEY CHECK(id=1), attempted_at TEXT, last_error TEXT NOT NULL DEFAULT '', attempted_path TEXT)")
            connection.execute("INSERT OR IGNORE INTO source_status(id) VALUES (1)")
            if "revision" not in {row[1] for row in connection.execute("PRAGMA table_info(owner_settings)")}:
                connection.execute("ALTER TABLE owner_settings ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE TABLE IF NOT EXISTS schedule_revision (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO schedule_revision VALUES (1, 0)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS reminder_events (
                    owner_id INTEGER NOT NULL,
                    event_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_after REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(owner_id, event_key)
                )
            """)

    async def bind_owner(self, telegram_id: int, chat_id: int) -> None:
        await asyncio.to_thread(self._bind_owner, telegram_id, chat_id)

    def _bind_owner(self, telegram_id: int, chat_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                INSERT INTO owner_settings (telegram_id, chat_id)
                VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    revision = owner_settings.revision + 1,
                    updated_at = CURRENT_TIMESTAMP
            """, (telegram_id, chat_id))

    async def replace_schedule(self, source: str, is_demo: bool, lessons: list[Lesson], *, source_path: str | None = None, source_hash: str | None = None) -> None:
        payloads = [(lesson.group, lesson.subgroup, json.dumps(asdict(lesson), ensure_ascii=False)) for lesson in lessons]
        await asyncio.to_thread(self._replace_schedule, source, is_demo, payloads, source_path, source_hash)

    def _replace_schedule(self, source: str, is_demo: bool, payloads: list[tuple], source_path: str | None, source_hash: str | None) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM lessons")
            connection.executemany("INSERT INTO lessons(group_name, subgroup, payload) VALUES (?, ?, ?)", payloads)
            connection.execute("""
                INSERT INTO schedule_info(id, source, is_demo, source_path, source_hash, imported_at)
                VALUES (1, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(id) DO UPDATE SET source=excluded.source,
                    is_demo=excluded.is_demo, imported_at=excluded.imported_at,
                    source_path=excluded.source_path, source_hash=excluded.source_hash
            """, (source, int(is_demo), source_path, source_hash))
            connection.execute("UPDATE source_status SET last_error='', attempted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), attempted_path=? WHERE id=1", (source_path,))
            connection.execute("UPDATE schedule_revision SET revision=revision+1 WHERE id=1")

    async def get_schedule(self, group: str = "141", subgroup: int = 1) -> tuple[dict | None, list[Lesson]]:
        return await asyncio.to_thread(self._get_schedule, group, subgroup)

    def _get_schedule(self, group: str, subgroup: int) -> tuple[dict | None, list[Lesson]]:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            # Одна транзакція: метадані та заняття належать одній версії.
            connection.execute("BEGIN")
            info = connection.execute("SELECT s.*, t.last_error, t.attempted_at FROM schedule_info s JOIN source_status t ON t.id=s.id WHERE s.id=1").fetchone()
            rows = connection.execute("SELECT payload FROM lessons WHERE group_name=? AND (subgroup=? OR subgroup IS NULL)", (group, subgroup)).fetchall()
            return (dict(info) if info else None, [Lesson(**json.loads(row["payload"])) for row in rows])

    async def get_owner_settings(self, owner_id: int) -> dict | None:
        return await asyncio.to_thread(self._get_owner_settings, owner_id)

    def _get_owner_settings(self, owner_id: int) -> dict | None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM owner_settings WHERE telegram_id=?", (owner_id,)).fetchone()
            return dict(row) if row else None

    async def update_owner_settings(self, owner_id: int, *, enabled: bool | None = None, minutes: int | None = None) -> None:
        if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= 1440):
            raise ValueError("Інтервал має бути від 1 до 1440 хвилин.")
        await asyncio.to_thread(self._update_owner_settings, owner_id, enabled, minutes)

    def _update_owner_settings(self, owner_id: int, enabled: bool | None, minutes: int | None) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                UPDATE owner_settings SET reminders_enabled=COALESCE(?, reminders_enabled),
                    reminder_minutes=COALESCE(?, reminder_minutes), revision=revision+1,
                    updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?
            """, (int(enabled) if enabled is not None else None, minutes, owner_id))

    async def reminder_snapshot(self, owner_id: int) -> tuple[dict | None, dict | None, list[Lesson]]:
        return await asyncio.to_thread(self._reminder_snapshot, owner_id)

    def _reminder_snapshot(self, owner_id: int) -> tuple[dict | None, dict | None, list[Lesson]]:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            owner = connection.execute("SELECT * FROM owner_settings WHERE telegram_id=?", (owner_id,)).fetchone()
            info = connection.execute("SELECT s.*, r.revision, t.last_error FROM schedule_info s JOIN schedule_revision r ON r.id=s.id JOIN source_status t ON t.id=s.id WHERE s.id=1").fetchone()
            rows = connection.execute("SELECT payload FROM lessons WHERE group_name='141' AND (subgroup=1 OR subgroup IS NULL)").fetchall()
            return dict(owner) if owner else None, dict(info) if info else None, [Lesson(**json.loads(row[0])) for row in rows]

    async def claim_reminder(self, owner_id: int, event_key: str, schedule_revision: int, settings_revision: int, now: float) -> bool:
        return await asyncio.to_thread(self._claim_reminder, owner_id, event_key, schedule_revision, settings_revision, now)

    def _claim_reminder(self, owner_id: int, event_key: str, schedule_revision: int, settings_revision: int, now: float) -> bool:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT reminders_enabled, revision FROM owner_settings WHERE telegram_id=?", (owner_id,)).fetchone()
            revision = connection.execute("SELECT revision FROM schedule_revision WHERE id=1").fetchone()
            if owner != (1, settings_revision) or revision != (schedule_revision,):
                return False
            cursor = connection.execute("""
                INSERT INTO reminder_events(owner_id, event_key, status) VALUES (?, ?, 'sending')
                ON CONFLICT(owner_id, event_key) DO UPDATE SET status='sending', updated_at=CURRENT_TIMESTAMP
                WHERE reminder_events.status='retry' AND reminder_events.retry_after<=?
            """, (owner_id, event_key, now))
            return cursor.rowcount == 1

    async def finish_reminder(self, owner_id: int, event_key: str, status: str, retry_after: float = 0) -> None:
        await asyncio.to_thread(self._finish_reminder, owner_id, event_key, status, retry_after)

    def _finish_reminder(self, owner_id: int, event_key: str, status: str, retry_after: float) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                UPDATE reminder_events SET status=?, retry_after=?, updated_at=CURRENT_TIMESTAMP
                WHERE owner_id=? AND event_key=?
            """, (status, retry_after, owner_id, event_key))

    async def mark_source_failure(self, error: str, path: str, *, expected_source: tuple[str, str] | None = None) -> None:
        await asyncio.to_thread(self._mark_source_failure, error, path, expected_source)

    def _mark_source_failure(self, error: str, path: str, expected_source: tuple[str, str] | None) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            condition = " AND EXISTS (SELECT 1 FROM schedule_info WHERE source_path=? AND source_hash=?)" if expected_source else ""
            connection.execute("UPDATE source_status SET last_error=?, attempted_path=?, attempted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1" + condition,
                               (error, path, *(expected_source or ())))

    async def get_source_status(self) -> dict:
        return await asyncio.to_thread(self._get_source_status)

    def _get_source_status(self) -> dict:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT t.*, s.source, s.imported_at, s.is_demo, s.source_path FROM source_status t LEFT JOIN schedule_info s ON s.id=t.id WHERE t.id=1").fetchone()
            return dict(row)
