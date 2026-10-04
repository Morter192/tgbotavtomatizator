"""Запуск: python -m tgbot."""

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from tgbot.config import ConfigError, load_settings, load_bot_token, storage_paths
from tgbot.database import Database
from tgbot.handlers import create_router
from tgbot.schedule import read_schedule
from tgbot.reminders import ReminderService
from tgbot.xls_source import SourceError, monitor_source, update_source, read_xls
from tgbot.logging_utils import configure_logging
from tgbot.schedule import format_day, lessons_for_day
from datetime import date, timedelta

logger = logging.getLogger(__name__)


async def run_services(bot, dispatcher, database: Database, owner_id: int) -> None:
    tasks = [
        asyncio.create_task(ReminderService(database, bot, owner_id).run(), name="reminders"),
        asyncio.create_task(monitor_source(database), name="schedule_source"),
        asyncio.create_task(dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False), name="polling"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
            if task.get_name() != "polling":
                raise RuntimeError("Фонова служба бота несподівано завершилася.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def show_user_ids() -> None:
    token = load_bot_token()
    configure_logging(token)
    async with Bot(token=token) as bot:
        updates = await bot.get_updates(limit=100, timeout=0)
    ids = sorted({update.message.from_user.id for update in updates if update.message and update.message.chat.type == "private" and update.message.from_user})
    for user_id in ids:
        print(f"ID приватного користувача: {user_id}")
    if not ids:
        print("Надішліть нове повідомлення своєму боту й повторіть команду. Основний бот має бути зупинений.")


async def run(check_only: bool = False, schedule_file: Path | None = None, source_file: Path | None = None) -> None:
    database_path, _ = storage_paths()
    database = Database(database_path)
    await database.initialize()
    if source_file is not None:
        count = await update_source(database, source_file)
        logger.info("XLS оновлено: %s записів для 141(1).", count)
        return
    if schedule_file is not None:
        source, is_demo, lessons = read_schedule(schedule_file)
        await database.replace_schedule(source, is_demo, lessons)
        logger.info("Збережено %s занять. Тип розкладу: %s.", len(lessons), "ТЕСТОВИЙ" if is_demo else "звичайний")
        return
    settings = load_settings()
    configure_logging(settings.bot_token)
    if check_only:
        logger.info("Конфігурацію та SQLite перевірено. З’єднання з Telegram не виконувалося.")
        return
    dispatcher = Dispatcher(settings=settings, database=database)
    dispatcher.include_router(create_router(settings.owner_telegram_id))
    async with Bot(token=settings.bot_token) as bot:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Підключити особистий чат"),
                BotCommand(command="help", description="Довідка про бота"),
                BotCommand(command="today", description="Пари сьогодні"),
                BotCommand(command="tomorrow", description="Пари завтра"),
                BotCommand(command="week", description="Розклад поточного тижня"),
                BotCommand(command="next", description="Найближча майбутня пара"),
                BotCommand(command="settings", description="Налаштування нагадувань"),
                BotCommand(command="status", description="Стан джерела розкладу"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        logger.info("Бот запускається. Для зупинки натисніть Ctrl+C.")
        await run_services(bot, dispatcher, database, settings.owner_telegram_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Особистий бот розкладу ЧНУ")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Перевірити .env і SQLite без Telegram")
    modes.add_argument("--import-schedule", type=Path, help="Замінити розклад даними з JSON без Telegram")
    modes.add_argument("--update-source", nargs="?", const="", metavar="PATH", help="Оновити XLS з SCHEDULE_SOURCE або вказаного шляху без Telegram")
    modes.add_argument("--preview-source", nargs="?", const="", metavar="PATH", help="Показати два типи тижня з XLS без зміни бази")
    modes.add_argument("--show-user-ids", action="store_true", help="Показати ID приватних користувачів з отриманих повідомлень без прив’язки доступу")
    args = parser.parse_args()
    configure_logging()
    try:
        if args.show_user_ids:
            asyncio.run(show_user_ids())
            return
        if args.preview_source is not None:
            path = Path(args.preview_source).expanduser() if args.preview_source else storage_paths()[1]
            lessons, _ = read_xls(path)
            for first in (date(2026, 10, 5), date(2026, 10, 12)):
                for offset in range(5):
                    day = first + timedelta(days=offset)
                    print(format_day(day, lessons_for_day(lessons, day, False)))
                    print()
            return
        source_file = None
        if args.update_source is not None:
            source_file = Path(args.update_source).expanduser() if args.update_source else storage_paths()[1]
        asyncio.run(run(check_only=args.check, schedule_file=args.import_schedule, source_file=source_file))
    except SourceError as error:
        logger.error("%s Попередній розклад збережено.", error)
        sys.exit(1)
    except ConfigError as error:
        logger.error("%s", error)
        sys.exit(1)
    except ValueError:
        logger.error("Некоректний JSON розкладу. Перевірте формат, поля, час і дати. Попередній розклад збережено.")
        sys.exit(1)
    except (OSError, sqlite3.Error):
        logger.error("Не вдалося відкрити файл або SQLite. Перевірте шлях та права доступу.")
        sys.exit(1)
    except TelegramAPIError:
        logger.error("Помилка Telegram. Перевірте токен, мережу та чи запущена лише одна копія бота.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Бот зупинено.")
    except Exception:
        logger.exception("Бот зупинено через неочікувану помилку. Перевірте локальні журнали.")
        sys.exit(1)


if __name__ == "__main__":
    main()
