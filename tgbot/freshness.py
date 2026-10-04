"""Позначки останнього успішного оновлення та проблем джерела."""

from datetime import datetime, timezone
from tgbot.schedule import KYIV


def freshness_text(info: dict) -> str:
    timestamp = info.get("imported_at")
    if timestamp:
        updated = datetime.fromisoformat(timestamp)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        result = f"Останнє успішне оновлення: {updated.astimezone(KYIV):%d.%m.%Y %H:%M:%S} (Київ)."
    else:
        result = "Успішного оновлення ще не було."
    if info.get("last_error"):
        result += "\nДані можуть бути застарілими. " + info["last_error"]
    return result
