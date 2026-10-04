import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, Update, User

from tgbot.config import ConfigError, PROJECT_ROOT, Settings, load_settings
from tgbot.database import Database
from tgbot.handlers import create_router
from tgbot.schedule import Lesson

TOKEN = "123456:TEST_ONLY_NOT_A_REAL_TOKEN"
OWNER = 123


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if not isinstance(method, SendMessage):
            raise AssertionError(f"Unexpected method: {type(method).__name__}")
        self.sent.append(method)
        return Message(
            message_id=len(self.sent), date=datetime.now(timezone.utc),
            chat=Chat(id=method.chat_id, type="private"), text=method.text,
        )

    async def stream_content(self, url, **kwargs):
        raise AssertionError("Network is disabled in tests")
        yield b""


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "bot.sqlite3"
        self.database = Database(self.path)
        await self.database.initialize()
        self.session = FakeSession()
        self.bot = Bot(token=TOKEN, session=self.session)
        self.settings = Settings(TOKEN, OWNER, self.path, ZoneInfo("Europe/Kyiv"))
        self.dispatcher = Dispatcher(database=self.database, settings=self.settings)
        self.dispatcher.include_router(create_router(OWNER))

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.temp.cleanup()

    async def send(self, text, user_id=OWNER, chat_type="private"):
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id if chat_type == "private" else -100123, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Студент"),
            text=text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
            if text.startswith("/") else None,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))

    def rows(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute(
                "SELECT telegram_id, chat_id, reminders_enabled, reminder_minutes FROM owner_settings"
            ).fetchall()

    async def test_start_binds_owner(self):
        await self.send("/start")
        self.assertEqual(self.rows(), [(OWNER, OWNER, 1, 10)])
        self.assertIn("чат підключено", self.session.sent[-1].text)

    async def test_help_lists_only_available_commands(self):
        await self.send("/help")
        self.assertIn("/start", self.session.sent[-1].text)
        self.assertIn("/help", self.session.sent[-1].text)
        self.assertIn("/settings", self.session.sent[-1].text)

    async def test_stranger_cannot_bind_or_read_help(self):
        for command in ("/start", "/help", "/today", "/tomorrow", "/week", "/next", "/settings off", "/status"):
            await self.send(command, user_id=999)
            self.assertEqual(self.session.sent[-1].text, "Цей бот доступний лише його власнику.")
        self.assertEqual(self.rows(), [])

    async def test_owner_in_group_is_ignored(self):
        await self.send("/start", chat_type="group")
        self.assertEqual(self.session.sent, [])
        self.assertEqual(self.rows(), [])

    async def test_settings_require_start_and_persist(self):
        await self.send("/settings")
        self.assertIn("/start", self.session.sent[-1].text)
        await self.send("/start")
        await self.send("/settings off")
        await self.send("/settings minutes 30")
        restarted = Database(self.path)
        await restarted.initialize()
        owner = await restarted.get_owner_settings(OWNER)
        self.assertEqual((owner["reminders_enabled"], owner["reminder_minutes"]), (0, 30))
        await self.send("/settings on")
        self.assertIn("увімкнено", self.session.sent[-1].text)
        self.assertIn("30 хв", self.session.sent[-1].text)

    async def test_settings_reject_invalid_values(self):
        await self.send("/start")
        for command in ("/settings minutes 0", "/settings minutes 1441", "/settings minutes abc", "/settings minutes ²", "/settings off extra"):
            await self.send(command)
            self.assertIn("Використай", self.session.sent[-1].text)
        self.assertEqual(self.rows(), [(OWNER, OWNER, 1, 10)])

    async def test_unknown_command_gets_help_hint(self):
        await self.send("/unknown")
        self.assertIn("/help", self.session.sent[-1].text)

    async def test_unloaded_schedule_is_distinct_from_empty_day(self):
        await self.send("/today")
        self.assertIn("ще не завантажено", self.session.sent[-1].text)
        await self.database.replace_schedule("Порожній тестовий розклад", True, [])
        await self.send("/today")
        self.assertIn("пар немає", self.session.sent[-1].text)
        self.assertIn("ТЕСТОВИЙ РОЗКЛАД", self.session.sent[-1].text)

    async def test_schedule_commands_use_kyiv_dates_and_filter_profile(self):
        await self.database.replace_schedule("demo", True, [
            Lesson("141", 1, 0, 1, "Парна пара", "08:20", "09:40", week=2, building="8", room="216"),
            Lesson("141", 1, 0, 2, "Непарна пара", "09:50", "11:10", week=1),
            Lesson("141", None, 1, 3, "Спільна лекція", "11:30", "12:50"),
            Lesson("141", 2, 0, 1, "Чужа підгрупа", "08:20", "09:40"),
            Lesson("241", 1, 0, 1, "Чужа група", "08:20", "09:40"),
        ])
        # UTC ще неділя, у Києві вже понеділок.
        utc_now = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
        with patch("tgbot.handlers.datetime") as clock:
            clock.now.side_effect = lambda zone: utc_now.astimezone(zone)
            await self.send("/today")
            today = self.session.sent[-1].text
            self.assertIn("05.10.2026", today)
            self.assertIn("Парна пара", today)
            self.assertNotIn("Непарна пара", today)
            self.assertNotIn("Чужа", today)
            self.assertIn("Корпус: 8", today)
            self.assertIn("Аудиторія: 216", today)
            await self.send("/tomorrow")
            self.assertIn("06.10.2026", self.session.sent[-1].text)
            self.assertIn("Спільна лекція", self.session.sent[-1].text)
            await self.send("/week")
            self.assertIn("11.10.2026", self.session.sent[-1].text)
            await self.send("/next")
            self.assertIn("Парна пара", self.session.sent[-1].text)

    async def test_schedule_survives_restart(self):
        lesson = Lesson("141", 1, 0, 1, "Предмет", "08:20", "09:40")
        await self.database.replace_schedule("source", False, [lesson])
        restarted = Database(self.path)
        await restarted.initialize()
        info, lessons = await restarted.get_schedule()
        self.assertEqual(info["source"], "source")
        self.assertEqual(lessons, [lesson])

    async def test_status_and_schedule_show_stale_and_last_success(self):
        await self.database.replace_schedule("source", False, [])
        await self.database.mark_source_failure("Джерело недоступне.", "missing.xls")
        for command in ("/status", "/today"):
            await self.send(command)
            self.assertIn("Останнє успішне оновлення", self.session.sent[-1].text)
            self.assertIn("застарілими", self.session.sent[-1].text)

    async def test_long_week_messages_all_mark_demo(self):
        lessons = [Lesson("141", 1, day, number, "Тестовий предмет " + "А" * 200,
                          f"{7 + number:02}:00", f"{7 + number:02}:40")
                   for day in range(7) for number in range(1, 8)]
        await self.database.replace_schedule("demo", True, lessons)
        await self.send("/week")
        self.assertGreater(len(self.session.sent), 1)
        for message in self.session.sent:
            self.assertLessEqual(len(message.text), 3500)
            self.assertIn("ТЕСТОВИЙ РОЗКЛАД", message.text)

    async def test_restart_and_repeated_start_preserve_settings(self):
        await self.send("/start")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE owner_settings SET reminders_enabled=0, reminder_minutes=15")
        restarted = Database(self.path)
        await restarted.initialize()
        await restarted.bind_owner(OWNER, OWNER)
        self.assertEqual(self.rows(), [(OWNER, OWNER, 0, 15)])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / ".env"

    def tearDown(self):
        self.temp.cleanup()

    def test_dotenv_loads_and_environment_takes_precedence(self):
        self.env_file.write_text(f"BOT_TOKEN={TOKEN}\nOWNER_TELEGRAM_ID=123\n", encoding="utf-8")
        with patch.dict(os.environ, {"OWNER_TELEGRAM_ID": "456"}, clear=True):
            settings = load_settings(self.env_file)
        self.assertEqual(settings.owner_telegram_id, 456)
        self.assertEqual(settings.database_path, PROJECT_ROOT / "data/bot.sqlite3")
        self.assertEqual(settings.timezone.key, "Europe/Kyiv")
        self.assertNotIn(TOKEN, repr(settings))

    def test_invalid_config_does_not_disclose_token(self):
        invalid_cases = [
            {"BOT_TOKEN": "SECRET_INVALID_TOKEN", "OWNER_TELEGRAM_ID": "123"},
            {"BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "@name"},
            {"BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "0"},
            {"BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "123", "TIMEZONE": "Wrong/Zone"},
            {"BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "123", "TIMEZONE": "Europe/Paris"},
            {"BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "123", "DATABASE_PATH": ""},
        ]
        for values in invalid_cases:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ConfigError) as error:
                    load_settings(self.env_file)
                self.assertNotIn(values["BOT_TOKEN"], str(error.exception))


if __name__ == "__main__":
    unittest.main()
