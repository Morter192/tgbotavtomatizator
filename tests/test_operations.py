import asyncio
import logging
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tgbot.__main__ import run_services, show_user_ids
from tgbot.logging_utils import RedactingFormatter


class LoggingTests(unittest.TestCase):
    def test_token_is_redacted_from_message_and_exception(self):
        token = "987654321" + ":" + "x" * 35
        try:
            raise RuntimeError("https://api.telegram.org/bot" + token + "/sendMessage")
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Failure %s", (token,), sys.exc_info())
        output = RedactingFormatter(token).format(record)
        self.assertNotIn(token, output)
        self.assertIn("[TOKEN]", output)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_stops_polling_and_propagates(self):
        cancelled = asyncio.Event()
        async def polling(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        dispatcher = SimpleNamespace(start_polling=polling, resolve_used_update_types=lambda: ["message"])
        async def monitor(*args):
            await asyncio.Future()
        with patch("tgbot.__main__.ReminderService") as service, patch("tgbot.__main__.monitor_source", side_effect=monitor):
            service.return_value.run = AsyncMock(side_effect=RuntimeError("worker failed"))
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                await run_services(object(), dispatcher, object(), 123)
        self.assertTrue(cancelled.is_set())

    async def test_polling_stop_cancels_background_workers(self):
        stopped = []
        async def worker(name):
            try:
                await asyncio.Future()
            finally:
                stopped.append(name)
        dispatcher = SimpleNamespace(start_polling=AsyncMock(), resolve_used_update_types=lambda: ["message"])
        async def monitor(database):
            await worker("source")
        with patch("tgbot.__main__.ReminderService") as service, patch("tgbot.__main__.monitor_source", side_effect=monitor):
            # Звичайний Mock повертає корутину служби, яку очікує create_task.
            from unittest.mock import Mock
            service.return_value.run = Mock(side_effect=lambda: worker("reminders"))
            await run_services(object(), dispatcher, object(), 123)
        self.assertEqual(set(stopped), {"reminders", "source"})

    async def test_id_discovery_does_not_bind_owner_or_send_messages(self):
        user = SimpleNamespace(id=123)
        private = SimpleNamespace(message=SimpleNamespace(from_user=user, chat=SimpleNamespace(type="private")))
        group = SimpleNamespace(message=SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(type="group")))
        bot = SimpleNamespace(get_updates=AsyncMock(return_value=[private, group]))
        with patch("tgbot.__main__.load_bot_token", return_value="123456:TEST_ONLY_NOT_A_REAL_TOKEN"), patch("tgbot.__main__.configure_logging"), patch("tgbot.__main__.Bot") as bot_class, patch("builtins.print") as output:
            bot_class.return_value.__aenter__.return_value = bot
            await show_user_ids()
        output.assert_called_once_with("ID приватного користувача: 123")
