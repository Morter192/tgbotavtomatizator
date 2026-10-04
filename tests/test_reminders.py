import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from tgbot.database import Database
from tgbot.reminders import ReminderService, occurrence_key
from tgbot.schedule import KYIV, Lesson, ONS, next_lesson

OWNER = 123


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.database = Database(self.path)
        await self.database.initialize()
        await self.database.bind_owner(OWNER, OWNER)
        self.lesson = Lesson("141", 1, 0, 1, "Програмування", "08:20", "09:40", week=2, building="8", room="216")
        await self.database.replace_schedule("test", True, [self.lesson])
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.service = ReminderService(self.database, self.bot, OWNER)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def now(self, hour=8, minute=10, second=0, day=5):
        return datetime(2026, 10, day, hour, minute, second, tzinfo=KYIV)

    async def test_due_boundary_and_no_repeats(self):
        await self.service.tick(self.now(8, 9, 59))
        self.bot.send_message.assert_not_awaited()
        await self.service.tick(self.now())
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.call_args.args[1]
        for value in ("Програмування", "08:20", "Корпус: 8", "Аудиторія: 216", "ТЕСТОВИЙ РОЗКЛАД"):
            self.assertIn(value, text)
        await self.service.tick(self.now(8, 11))
        self.bot.send_message.assert_awaited_once()

    async def test_at_or_after_start_never_sends(self):
        await self.service.tick(self.now(8, 20))
        await self.service.tick(self.now(8, 21))
        self.bot.send_message.assert_not_awaited()

    async def test_restart_does_not_repeat_sent_or_reimported_lesson(self):
        await self.service.tick(self.now())
        restarted = Database(self.path)
        await restarted.initialize()
        await restarted.replace_schedule("new source", True, [replace(self.lesson, room="117", start="08:25")])
        await ReminderService(restarted, self.bot, OWNER).tick(self.now(8, 16))
        self.bot.send_message.assert_awaited_once()

    async def test_restart_catches_due_before_start(self):
        restarted = Database(self.path)
        await restarted.initialize()
        await ReminderService(restarted, self.bot, OWNER).tick(self.now(8, 15))
        self.bot.send_message.assert_awaited_once()
        self.assertIn("5 хв", self.bot.send_message.call_args.args[1])

    async def test_concurrent_workers_claim_only_once(self):
        other = ReminderService(Database(self.path), self.bot, OWNER)
        await asyncio.gather(self.service.tick(self.now()), other.tick(self.now()))
        self.bot.send_message.assert_awaited_once()

    async def test_disabled_and_changed_minutes(self):
        await self.database.update_owner_settings(OWNER, enabled=False, minutes=20)
        await self.service.tick(self.now(8, 0))
        self.bot.send_message.assert_not_awaited()
        await self.database.update_owner_settings(OWNER, enabled=True)
        await self.service.tick(self.now(7, 59, 59))
        self.bot.send_message.assert_not_awaited()
        await self.service.tick(self.now(8, 0))
        self.bot.send_message.assert_awaited_once()

    async def test_schedule_update_changes_unsent_time_and_room(self):
        await self.service.tick(self.now(8, 0))
        await self.database.replace_schedule("updated", True, [replace(self.lesson, start="09:00", room="117")])
        await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()
        await self.service.tick(self.now(8, 50))
        self.bot.send_message.assert_awaited_once()
        self.assertIn("09:00", self.bot.send_message.call_args.args[1])
        self.assertIn("Аудиторія: 117", self.bot.send_message.call_args.args[1])

    async def test_cancelled_date_and_removed_lesson(self):
        await self.database.replace_schedule("cancelled", True, [replace(self.lesson, cancelled_dates=("2026-10-05",))])
        await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(next_lesson([replace(self.lesson, cancelled_dates=("2026-10-05", "2026-10-19"))], self.now(8, 0), True)[0], date(2026, 11, 2))
        await self.database.replace_schedule("empty", True, [])
        await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()

    async def test_update_between_snapshot_and_claim_rejects_stale_message(self):
        original = self.database.claim_reminder
        async def change_then_claim(*args):
            await self.database.replace_schedule("cancelled", True, [])
            return await original(*args)
        with patch.object(self.database, "claim_reminder", side_effect=change_then_claim):
            await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()

    async def test_settings_change_between_snapshot_and_claim_rejects_stale_message(self):
        original = self.database.claim_reminder
        async def disable_then_claim(*args):
            await self.database.update_owner_settings(OWNER, enabled=False)
            return await original(*args)
        with patch.object(self.database, "claim_reminder", side_effect=disable_then_claim):
            await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()

    async def test_parity_and_midnight_week_boundary(self):
        await self.service.tick(self.now(day=12))
        self.bot.send_message.assert_not_awaited()
        early = replace(self.lesson, start="00:05", end="01:00")
        await self.database.replace_schedule("early", True, [early])
        await self.service.tick(datetime(2026, 10, 4, 23, 55, tzinfo=KYIV))
        self.bot.send_message.assert_awaited_once()
        self.assertIn("05.10.2026", self.bot.send_message.call_args.args[1])

    async def test_dst_uses_elapsed_minutes_and_online_link(self):
        await self.database.update_owner_settings(OWNER, minutes=1440)
        lesson = replace(self.lesson, weekday=6, week=0, start="08:00", end="09:00", online=True, url="https://example.com/class")
        await self.database.replace_schedule("online", True, [lesson])
        # Неділя 25.10 о 08:00 EET: за 24 години — субота о 09:00 EEST.
        await self.service.tick(datetime(2026, 10, 24, 5, 59, 59, tzinfo=timezone.utc))
        self.bot.send_message.assert_not_awaited()
        await self.service.tick(datetime(2026, 10, 24, 6, tzinfo=timezone.utc))
        self.bot.send_message.assert_awaited_once()
        self.assertIn("https://example.com/class", self.bot.send_message.call_args.args[1])

    async def test_unconfirmed_lesson_is_not_sent(self):
        await self.database.replace_schedule("unconfirmed", True, [replace(self.lesson, time_confirmed=False)])
        await self.service.tick(self.now())
        self.bot.send_message.assert_not_awaited()

    async def test_confirmed_ons_reminders_only_on_odd_friday_without_repeats(self):
        await self.database.replace_schedule("real", False, [])
        for day in (9, 23, 30):
            await self.service.tick(self.now(12, 50, day=day))
        self.bot.send_message.assert_not_awaited()
        for hour, minute, start in ((12, 50, "13:00"), (14, 30, "14:40"), (16, 0, "16:10")):
            await self.service.tick(self.now(hour, minute, day=16))
            text = self.bot.send_message.call_args.args[1]
            self.assertIn(ONS, text)
            self.assertIn(start, text)
            self.assertIn("посилання не вказано", text)
            await self.service.tick(self.now(hour, minute, day=16))
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_spring_dst_horizon_reaches_day_after_tomorrow(self):
        await self.database.update_owner_settings(OWNER, minutes=1440)
        lesson = replace(self.lesson, week=0, start="00:05", end="01:00")
        await self.database.replace_schedule("early after DST", True, [lesson])
        await self.service.tick(datetime(2027, 3, 27, 21, 5, tzinfo=timezone.utc))
        self.bot.send_message.assert_awaited_once()
        self.assertIn("29.03.2027", self.bot.send_message.call_args.args[1])

    async def test_network_uncertainty_does_not_repeat_after_restart(self):
        self.bot.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="test"), message="test timeout")
        await self.service.tick(self.now())
        await ReminderService(Database(self.path), self.bot, OWNER).tick(self.now(8, 11))
        self.bot.send_message.assert_awaited_once()

    async def test_rate_limit_retries_only_after_delay_and_before_start(self):
        self.bot.send_message.side_effect = [TelegramRetryAfter(method=SendMessage(chat_id=OWNER, text="test"), message="rate limit", retry_after=60), None]
        await self.service.tick(self.now())
        await self.service.tick(self.now(8, 10, 59))
        self.bot.send_message.assert_awaited_once()
        await self.service.tick(self.now(8, 11))
        self.assertEqual(self.bot.send_message.await_count, 2)
        await self.service.tick(self.now(8, 12))
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_crash_after_reservation_does_not_repeat(self):
        owner, info, _ = await self.database.reminder_snapshot(OWNER)
        key = occurrence_key(self.now().date(), self.lesson, True)
        self.assertTrue(await self.database.claim_reminder(OWNER, key, info["revision"], owner["revision"], self.now().timestamp()))
        await ReminderService(Database(self.path), self.bot, OWNER).tick(self.now(8, 11))
        self.bot.send_message.assert_not_awaited()

    async def test_legacy_database_migrates_without_resetting_settings(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("CREATE TABLE owner_settings (telegram_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, reminders_enabled INTEGER NOT NULL, reminder_minutes INTEGER NOT NULL, updated_at TEXT)")
            connection.execute("INSERT INTO owner_settings VALUES (123,123,0,30,'old')")
        legacy = Database(legacy_path)
        await legacy.initialize()
        owner = await legacy.get_owner_settings(OWNER)
        self.assertEqual((owner["reminders_enabled"], owner["reminder_minutes"], owner["revision"]), (0, 30, 0))
