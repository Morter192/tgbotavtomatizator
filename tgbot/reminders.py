"""Нагадування з перевіркою актуального розкладу та журналом SQLite."""

import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from tgbot.database import Database
from tgbot.schedule import KYIV, Lesson, format_lesson, lessons_for_day
from tgbot.freshness import freshness_text

logger = logging.getLogger(__name__)


def occurrence_key(day: date, lesson: Lesson, is_demo: bool) -> str:
    # Час та аудиторія можуть змінитися; вже надіслане нагадування
    # для цього заняття не повторюється після таких змін або імпорту.
    identity = [day.isoformat(), lesson.group, lesson.number, lesson.subject.strip(), is_demo]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


class ReminderService:
    def __init__(self, database: Database, bot: Bot, owner_id: int):
        self.database = database
        self.bot = bot
        self.owner_id = owner_id

    async def tick(self, now: datetime | None = None) -> None:
        live_clock = now is None
        now = now or datetime.now(KYIV)
        if now.tzinfo is None:
            raise ValueError("Час нагадувань має містити часовий пояс.")
        now = now.astimezone(KYIV)
        owner, info, lessons = await self.database.reminder_snapshot(self.owner_id)
        if not owner or not info or not owner["reminders_enabled"]:
            return
        minutes = owner["reminder_minutes"]
        is_demo = bool(info["is_demo"])
        # Горизонт обчислюється за реальними хвилинами: при переході
        # на літній час 24 години можуть охопити три календарні дні.
        horizon = (now.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(KYIV).date()
        for offset in range((horizon - now.date()).days + 1):
            day = now.date() + timedelta(days=offset)
            for lesson in lessons_for_day(lessons, day, is_demo):
                if not lesson.time_confirmed:
                    continue
                starts_at = datetime.combine(day, time.fromisoformat(lesson.start), KYIV).astimezone(timezone.utc)
                current = datetime.now(KYIV) if live_clock else now
                utc_now = current.astimezone(timezone.utc)
                due_at = starts_at - timedelta(minutes=minutes)
                if not due_at <= utc_now < starts_at:
                    continue
                key = occurrence_key(day, lesson, is_demo)
                if not await self.database.claim_reminder(self.owner_id, key, info["revision"], owner["revision"], current.timestamp()):
                    continue
                if live_clock:
                    utc_now = datetime.now(timezone.utc)
                    if utc_now >= starts_at:
                        await self.database.finish_reminder(self.owner_id, key, "expired")
                        continue
                banner = "ТЕСТОВИЙ РОЗКЛАД — вигадані дані\n" if is_demo else ""
                remaining = max(1, int((starts_at - utc_now).total_seconds() / 60))
                freshness = "\n" + freshness_text(info) if info.get("last_error") else ""
                text = f"{banner}Нагадування · {day:%d.%m.%Y}\nДо початку приблизно {remaining} хв.\nЧНУ · група 141 · підгрупа 1{freshness}\n\n{format_lesson(lesson)}"
                try:
                    await self.bot.send_message(owner["chat_id"], text)
                except TelegramRetryAfter as error:
                    await self.database.finish_reminder(self.owner_id, key, "retry", utc_now.timestamp() + error.retry_after)
                    logger.warning("Telegram обмежив частоту надсилання; повтор після дозволеної затримки.")
                except (TelegramBadRequest, TelegramForbiddenError):
                    await self.database.finish_reminder(self.owner_id, key, "failed")
                    logger.warning("Telegram відхилив нагадування. Перевірте доступ до бота.")
                except (TelegramAPIError, OSError, asyncio.TimeoutError):
                    # Telegram міг прийняти запит до втрати відповіді.
                    # Повтор у такій ситуації міг би створити дублікат.
                    await self.database.finish_reminder(self.owner_id, key, "uncertain")
                    logger.warning("Результат надсилання невідомий; автоматичний повтор вимкнено для уникнення дубліката.")
                else:
                    await self.database.finish_reminder(self.owner_id, key, "sent")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except (sqlite3.Error, OSError):
                logger.error("Не вдалося перевірити нагадування; наступна перевірка за 5 секунд.")
            await asyncio.sleep(5)
