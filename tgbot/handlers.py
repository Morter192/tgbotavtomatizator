"""Команди та перевірка доступу власника."""

from collections.abc import Awaitable, Callable
from typing import Any
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, TelegramObject

from tgbot.config import Settings
from tgbot.database import Database
from tgbot.schedule import KYIV, format_day, format_lesson, lessons_for_day, next_lesson
from tgbot.freshness import freshness_text

HELP_TEXT = (
    "Доступні команди:\n"
    "/start — прив’язати твій особистий чат\n"
    "/help — показати цю довідку\n"
    "/today — пари сьогодні\n"
    "/tomorrow — пари завтра\n"
    "/week — розклад поточного тижня\n"
    "/next — найближча майбутня пара\n"
    "/settings — налаштування нагадувань\n\n"
    "/status — джерело та час оновлення\n\n"
    "Бот для ЧНУ, групи 141, першої підгрупи.\n"
    "Нагадування: стандартно за 10 хвилин. Змінити: /settings minutes 15."
)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return None
        if event.chat.type != ChatType.PRIVATE:
            return None
        if event.from_user is None or event.from_user.id != self.owner_id:
            await event.answer("Цей бот доступний лише його власнику.")
            return None
        return await handler(event, data)


def create_router(owner_id: int) -> Router:
    router = Router(name="personal_bot")
    router.message.outer_middleware(OwnerOnlyMiddleware(owner_id))

    @router.message(CommandStart())
    async def start(message: Message, database: Database, settings: Settings) -> None:
        await database.bind_owner(settings.owner_telegram_id, message.chat.id)
        await message.answer(
            "Привіт! Твій особистий чат підключено.\n"
            "Я допомагатиму з розкладом ЧНУ для групи 141, першої підгрупи.\n\n"
            "Розклад: /today, /tomorrow, /week, /next. Довідка: /help.\n"
            "Нагадування підключено. Перевірити й змінити: /settings."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("status"))
    async def status_command(message: Message, database: Database) -> None:
        from tgbot.xls_source import check_source
        await check_source(database)
        status = await database.get_source_status()
        name = status.get("source") or "ще не підключене"
        await message.answer(f"Джерело: {name}\n" + freshness_text(status))

    @router.message(Command("settings"))
    async def settings_command(message: Message, database: Database, settings: Settings) -> None:
        owner = await database.get_owner_settings(settings.owner_telegram_id)
        if owner is None:
            await message.answer("Спочатку надішли /start, щоб підключити особистий чат.")
            return
        args = (message.text or "").split()[1:]
        if args:
            if args in (["on"], ["off"]):
                await database.update_owner_settings(settings.owner_telegram_id, enabled=args[0] == "on")
            elif len(args) == 2 and args[0] == "minutes" and len(args[1]) <= 4 and args[1].isascii() and args[1].isdigit() and 1 <= int(args[1]) <= 1440:
                await database.update_owner_settings(settings.owner_telegram_id, minutes=int(args[1]))
            else:
                await message.answer("Використай /settings on, /settings off або /settings minutes N, де N — від 1 до 1440 хвилин.")
                return
            owner = await database.get_owner_settings(settings.owner_telegram_id)
        state = "увімкнено" if owner["reminders_enabled"] else "вимкнено"
        await message.answer(
            f"Сповіщення: {state}.\nНагадувати за {owner['reminder_minutes']} хв до пари.\n"
            "Часовий пояс: Europe/Kyiv.\n\n"
            "/settings on — увімкнути\n/settings off — вимкнути\n"
            "/settings minutes 15 — нагадувати за 15 хв\n"
            "Налаштування зберігаються після перезапуску."
        )

    @router.message(Command("today", "tomorrow", "week", "next"))
    async def schedule_command(message: Message, database: Database) -> None:
        info, lessons = await database.get_schedule()
        if info is None:
            await message.answer("Розклад ще не завантажено. Надішли актуальний файл у чат із Codex для додавання розкладу.")
            return
        now = datetime.now(KYIV)
        today = now.date()
        command = (message.text or "").split()[0].split("@")[0]
        is_demo = bool(info["is_demo"])
        if command == "/next":
            upcoming = next_lesson(lessons, now, is_demo)
            if upcoming:
                day, lesson = upcoming
                body = f"Найближча пара · {day:%d.%m.%Y}\n\n" + format_lesson(lesson)
            else:
                body = "У завантаженому розкладі майбутніх пар немає."
        elif command == "/week":
            monday = today - timedelta(days=today.weekday())
            days = [monday + timedelta(days=offset) for offset in range(7)]
            body = "\n\n".join(format_day(day, lessons_for_day(lessons, day, is_demo)) for day in days)
        else:
            day = today + timedelta(days=1 if command == "/tomorrow" else 0)
            body = format_day(day, lessons_for_day(lessons, day, is_demo))
        prefix = ("ТЕСТОВИЙ РОЗКЛАД — вигадані дані\n" if is_demo else "") + "ЧНУ · група 141 · підгрупа 1\n" + freshness_text(info) + "\n\n"
        # Telegram обмежує довжину повідомлення; кожна частина має позначку джерела.
        chunk = prefix
        for line in body.splitlines(keepends=True):
            if len(chunk) + len(line) > 3500:
                await message.answer(chunk.rstrip())
                chunk = prefix
            chunk += line
        if chunk.strip():
            await message.answer(chunk.rstrip())

    @router.message()
    async def unknown_message(message: Message) -> None:
        await message.answer("Скористайся /start або /help, щоб побачити доступні можливості.")

    return router
