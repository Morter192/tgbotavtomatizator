"""Імпорт локального XLS кафедри МПУіК, без виконання вмісту комірок."""

import asyncio
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path

import xlrd
from xlrd.compdoc import CompDocError

from tgbot.database import Database
from tgbot.schedule import Lesson

DAY_NAMES = {"Понеділок": 0, "Вівторок": 1, "Середа": 2, "Четвер": 3, "П'ятниця": 4, "П’ятниця": 4}


class SourceError(ValueError):
    """Безпечний для відображення опис проблеми джерела."""


def parse_time(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2})[-:](\d{2})", value.strip())
    if not match or not 0 <= int(match[1]) <= 23 or not 0 <= int(match[2]) <= 59:
        raise SourceError("У розкладі відсутній або некоректний час пари.")
    return f"{int(match[1]):02}:{int(match[2]):02}"


def parse_sheet(sheet) -> list[Lesson]:
    headers = [(r, c) for r in range(sheet.nrows) for c in range(sheet.ncols) if str(sheet.cell_value(r, c)).strip() == "141(1)"]
    if len(headers) != 1:
        raise SourceError("Не знайдено єдиний заголовок підгрупи 141(1).")
    header_row, column = headers[0]
    if column < 4:
        raise SourceError("Структура колонок розкладу змінилася; потрібна перевірка імпорту.")

    def cell(row, col):
        if not 0 <= row < sheet.nrows or not 0 <= col < sheet.ncols:
            raise SourceError("Неповний блок пари у файлі.")
        for r0, r1, c0, c1 in sheet.merged_cells:
            if r0 <= row < r1 and c0 <= col < c1:
                return str(sheet.cell_value(r0, c0)).strip(), (r0, r1, c0, c1)
        return str(sheet.cell_value(row, col)).strip(), (row, row + 1, col, col + 1)

    day_rows = [(r, DAY_NAMES[str(sheet.cell_value(r, 0)).strip()]) for r in range(header_row + 1, sheet.nrows) if str(sheet.cell_value(r, 0)).strip() in DAY_NAMES]
    if [day for _, day in day_rows] != list(range(5)):
        raise SourceError("У файлі не знайдено послідовні блоки понеділка–п’ятниці.")
    lessons = []
    for day_index, (first, weekday) in enumerate(day_rows):
        last = day_rows[day_index + 1][0] if day_index + 1 < len(day_rows) else sheet.nrows
        if last - first != 42:
            raise SourceError("Очікуються 7 пар по 6 рядків на день; структура файлу змінилася.")
        for number in range(1, 8):
            base = first + (number - 1) * 6
            if cell(base, 1)[0] not in (str(number), f"{number}.0") or cell(base, 2)[0] != "1-й тижд." or cell(base + 3, 2)[0] != "2-й тижд.":
                raise SourceError("Номер пари або позначки тижня не відповідають структурі файлу.")
            start, end = parse_time(cell(base, 3)[0]), parse_time(cell(base + 3, 3)[0])
            top_subject, span = cell(base, column)
            weekly = bool(top_subject) and span[0] == base and span[1] - span[0] == 2
            parts = [(base, 0, 2)] if weekly else [(base, 1, 1), (base + 3, 2, 1)]
            for row, week, stride in parts:
                subject, subject_span = cell(row, column)
                if not subject:
                    # Не пропускаємо залишені викладача/місце без назви предмета.
                    if cell(row + stride, column)[0] or cell(row + stride * 2, column)[0]:
                        raise SourceError("Є викладач або місце без назви предмета; потрібна перевірка.")
                    continue
                if not isinstance(sheet.cell_value(subject_span[0], subject_span[2]), str):
                    raise SourceError("Назва предмета має бути текстом; можлива помилка комірки.")
                if subject_span[0] != row or subject_span[1] - subject_span[0] != stride:
                    raise SourceError("Неоднозначне об’єднання комірок предмета.")
                teacher, teacher_span = cell(row + stride, column)
                location, location_span = cell(row + stride * 2, column)
                for expected, value, value_span in ((row + stride, teacher, teacher_span), (row + stride * 2, location, location_span)):
                    if value and (value_span[0] != expected or value_span[1] - value_span[0] != stride):
                        raise SourceError("Неоднозначне розташування викладача або аудиторії.")
                valid_from = None
                if teacher.startswith("з "):
                    try:
                        valid_from = datetime.strptime(teacher[2:].strip(), "%d.%m.%Y").date().isoformat()
                    except ValueError:
                        raise SourceError("Не вдалося прочитати дату початку дії заняття.") from None
                    teacher = ""
                building, room = "", ""
                online = location.casefold() == "онлайн"
                room_match = re.fullmatch(r"(\d+)-(.+)", location)
                if room_match:
                    building, room = room_match.groups()
                # Спільність визначається об’єднанням саме з сусідньою підгрупою 141(2).
                shared = subject_span[2] <= column and subject_span[3] > column + 1 and str(sheet.cell_value(header_row, column + 1)).strip() == "141(2)"
                shared = shared and all(not value or value_span[3] > column + 1 for value, value_span in ((teacher, teacher_span), (location, location_span)))
                cells = f"{sheet.name}!{xlrd.formula.colname(column)}{row + 1}:{xlrd.formula.colname(subject_span[3] - 1)}{row + stride * 3};D{base + 1};D{base + 4}"
                lessons.append(Lesson("141", None if shared else 1, weekday, number, subject, start, end, week=week,
                                      teacher=teacher, building=building, room=room, location=location,
                                      online=online, valid_from=valid_from, source_cells=cells))
    if not lessons:
        raise SourceError("У файлі немає занять підгрупи 141(1); порожнє оновлення не активовано.")
    identities = [(lesson.weekday, lesson.number, lesson.week) for lesson in lessons]
    if len(set(identities)) != len(identities):
        raise SourceError("У файлі є дублікати занять для тієї самої пари й тижня.")
    return lessons


def read_xls(path: Path) -> tuple[list[Lesson], str]:
    try:
        content = path.read_bytes()
    except OSError:
        raise SourceError("Файл джерела недоступний. Дані можуть бути застарілими.") from None
    if xlrd.inspect_format(content=content) != "xls":
        raise SourceError("Джерело має бути справжнім Excel .xls; формат не розпізнано.")
    try:
        workbook = xlrd.open_workbook(file_contents=content, formatting_info=True, logfile=io.StringIO())
        if "МПУіК" not in workbook.sheet_names():
            raise SourceError("У файлі немає аркуша МПУіК.")
        lessons = parse_sheet(workbook.sheet_by_name("МПУіК"))
    except SourceError:
        raise
    except (xlrd.XLRDError, CompDocError, ValueError, IndexError, TypeError):
        raise SourceError("Файл пошкоджений або структура розкладу не підтримується.") from None
    return lessons, hashlib.sha256(content).hexdigest()


async def update_source(database: Database, path: Path) -> int:
    try:
        lessons, checksum = await asyncio.to_thread(read_xls, path)
    except SourceError as error:
        await database.mark_source_failure(str(error), str(path))
        raise
    # Старий формат ОНС відомий і замінюється особистим правилом.
    # Нові несумісні записи не можна непомітно відкинути.
    from tgbot.schedule import ONS
    for lesson in lessons:
        if lesson.subject.strip() == ONS and (lesson.weekday != 4 or lesson.week != 1 or lesson.number not in (1, 2, 3, 4) or lesson.valid_from != "2026-10-01"):
            error = SourceError("Нові записи ОНС суперечать узгодженому правилу; потрібне уточнення перед оновленням.")
            await database.mark_source_failure(str(error), str(path))
            raise error
    import logging
    from dataclasses import asdict
    _, old = await database.get_schedule()
    encode = lambda lesson: tuple(sorted((key, str(value)) for key, value in asdict(lesson).items() if key != "source_cells"))
    old_set, new_set = {encode(lesson) for lesson in old}, {encode(lesson) for lesson in lessons}
    logging.getLogger(__name__).info("Зміни перед активацією: додано %s, прибрано %s записів. Перегляд обох типів тижня: --preview-source.", len(new_set-old_set), len(old_set-new_set))
    await database.replace_schedule(path.name, False, lessons, source_path=str(path.resolve()), source_hash=checksum)
    return len(lessons)


async def check_source(database: Database) -> None:
    info, _ = await database.get_schedule()
    if not info or not info.get("source_path"):
        return
    path = Path(info["source_path"])
    try:
        checksum = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        await database.mark_source_failure("Файл джерела недоступний. Дані можуть бути застарілими.", str(path), expected_source=(info["source_path"], info["source_hash"]))
        return
    if checksum != info["source_hash"]:
        await database.mark_source_failure("Файл джерела змінився. Оновіть розклад; дані можуть бути застарілими.", str(path), expected_source=(info["source_path"], info["source_hash"]))


async def monitor_source(database: Database) -> None:
    import logging
    import sqlite3
    while True:
        try:
            await check_source(database)
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).error("Не вдалося перевірити доступність джерела розкладу.")
        await asyncio.sleep(60)
