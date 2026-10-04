"""Завантаження й перевірка конфігурації без розкриття секретів."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def storage_paths() -> tuple[Path, Path]:
    """Шляхи для локальних операцій, що не потребують токена Telegram."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    def resolve(name: str, default: str) -> Path:
        value = os.getenv(name, default).strip()
        if not value or value == ":memory:":
            raise ConfigError(f"{name} має бути шляхом до локального файлу.")
        path = Path(value).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return resolve("DATABASE_PATH", "data/bot.sqlite3"), resolve("SCHEDULE_SOURCE", "data/schedule.xls")


class ConfigError(ValueError):
    """Помилка, яку можна безпечно показати користувачу."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_telegram_id: int
    database_path: Path
    timezone: ZoneInfo


def load_bot_token(env_file: Path | None = None) -> str:
    load_dotenv(env_file if env_file is not None else PROJECT_ROOT / ".env", override=False)
    token = os.getenv("BOT_TOKEN", "").strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ConfigError("Заповніть BOT_TOKEN коректним токеном від BotFather у .env.") from None
    return token


def load_settings(env_file: Path | None = None) -> Settings:
    token = load_bot_token(env_file)
    try:
        owner_id = int(os.getenv("OWNER_TELEGRAM_ID", ""))
        if owner_id <= 0:
            raise ValueError
    except ValueError:
        raise ConfigError("OWNER_TELEGRAM_ID має бути додатним числовим Telegram ID.") from None
    try:
        timezone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv").strip())
        if timezone.key != "Europe/Kyiv":
            raise ValueError
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("Невідомий TIMEZONE. Використайте Europe/Kyiv.") from None
    raw_path = os.getenv("DATABASE_PATH", "data/bot.sqlite3").strip()
    if not raw_path or raw_path == ":memory:":
        raise ConfigError("DATABASE_PATH має бути шляхом до файлу SQLite.")
    database_path = Path(raw_path).expanduser()
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path
    return Settings(token, owner_id, database_path, timezone)
