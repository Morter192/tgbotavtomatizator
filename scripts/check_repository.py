"""Перевірка файлів, які Git може включити до коміту, без друку секретів."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")
OWNER_ID = re.compile(r"OWNER_TELEGRAM_ID\s*=\s*[\"']?\d{5,}")
TEST_TOKEN = "123456:TEST_ONLY_NOT_A_REAL_TOKEN"


def main() -> int:
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True)
    paths = sorted(set(result.stdout.decode().strip("\0").split("\0")))
    errors = []
    for name in paths:
        if not name:
            continue
        path = Path(name)
        if (path.name.startswith(".env") and path.name != ".env.example") or path.suffix.lower() in {".xls", ".xlsx", ".sqlite", ".sqlite3", ".db", ".csv", ".pem", ".key", ".p12"} or "data" in path.parts:
            errors.append((name, "приватний файл не має входити до коміту"))
            continue
        if path.suffix == ".json" and name != "examples/demo_schedule.json":
            errors.append((name, "JSON поза дозволеним демонстраційним прикладом"))
            continue
        content = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if any(match.group() != TEST_TOKEN for match in TOKEN.finditer(content)):
            errors.append((name, "схоже на Telegram-токен"))
        if PRIVATE_PATH.search(content):
            errors.append((name, "особистий шлях користувача"))
        if OWNER_ID.search(content):
            errors.append((name, "числовий ID власника"))
    for name, reason in errors:
        print(f"{name}: {reason}", file=sys.stderr)
    if not errors:
        print(f"Перевірено {len(paths)} файлів для коміту: приватних файлів, токенів та особистих шляхів не знайдено.")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
