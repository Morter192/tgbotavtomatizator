import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tgbot.database import Database
from tgbot.freshness import freshness_text
from tgbot.schedule import Lesson, lessons_for_day
from tgbot.xls_source import SourceError, check_source, parse_sheet, parse_time, read_xls, update_source


class FakeSheet:
    name = "МПУіК"
    nrows = 215
    ncols = 6

    def __init__(self):
        self.values = {(4, 4): "141(1)", (4, 5): "141(2)"}
        self.merged_cells = []
        for day, name in enumerate(("Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця")):
            first = 5 + day * 42
            self.values[first, 0] = name
            self.merged_cells.append((first, first + 42, 0, 1))
            for number, (start, end) in enumerate((("8-20", "9-40"), ("9-50", "11-10"), ("11-30", "12-50"), ("13-00", "14-20"), ("14-40", "16-00"), ("16-10", "17-30"), ("17-40", "19-00")), 1):
                row = first + (number - 1) * 6
                self.values[row, 1] = number
                self.values[row, 2] = "1-й тижд."
                self.values[row + 3, 2] = "2-й тижд."
                self.values[row, 3], self.values[row + 3, 3] = start, end

    def cell_value(self, row, column):
        return self.values.get((row, column), "")

    def add_weekly(self, row=5, shared=False, location="8-216"):
        for offset, value in ((0, "Щотижневий предмет"), (2, "Викладач"), (4, location)):
            self.values[row + offset, 4] = value
            self.merged_cells.append((row + offset, row + offset + 2, 4, 6 if shared else 5))


class ParserTests(unittest.TestCase):
    def test_merged_weekly_and_shared_subject(self):
        sheet = FakeSheet()
        sheet.add_weekly(shared=True)
        lessons = parse_sheet(sheet)
        self.assertEqual(len(lessons), 1)
        self.assertEqual((lessons[0].week, lessons[0].subgroup), (0, None))
        self.assertEqual((lessons[0].start, lessons[0].end, lessons[0].building, lessons[0].room), ("08:20", "09:40", "8", "216"))
        self.assertIn("МПУіК!", lessons[0].source_cells)

    def test_separate_weeks_and_no_invented_room(self):
        sheet = FakeSheet()
        sheet.values[5, 4], sheet.values[6, 4] = "Непарний предмет", "Викладач"
        sheet.values[8, 4], sheet.values[9, 4], sheet.values[10, 4] = "Парний предмет", "Викладач", "онлайн"
        lessons = parse_sheet(sheet)
        self.assertEqual([lesson.week for lesson in lessons], [1, 2])
        self.assertEqual((lessons[0].room, lessons[0].building, lessons[0].location), ("", "", ""))
        self.assertTrue(lessons[1].online)
        self.assertEqual([lesson.subject for lesson in lessons_for_day(lessons, date(2026, 10, 5), True)], ["Парний предмет"])

    def test_second_subgroup_excluded(self):
        sheet = FakeSheet()
        sheet.add_weekly()
        sheet.values[11, 5] = "Лише друга підгрупа"
        self.assertEqual(len(parse_sheet(sheet)), 1)

    def test_date_note_is_not_teacher(self):
        sheet = FakeSheet()
        sheet.values[5, 4], sheet.values[6, 4], sheet.values[7, 4] = "Предмет", "з 01.10.2026", "військова кафедра"
        lesson = parse_sheet(sheet)[0]
        self.assertEqual((lesson.teacher, lesson.valid_from, lesson.location), ("", "2026-10-01", "військова кафедра"))

    def test_changed_layout_and_missing_time_rejected(self):
        for problem in ("header", "time", "empty"):
            sheet = FakeSheet()
            if problem != "empty":
                sheet.add_weekly()
            if problem == "header":
                sheet.values[4, 4] = "other"
            if problem == "time":
                sheet.values[5, 3] = ""
            with self.subTest(problem=problem), self.assertRaises(SourceError):
                parse_sheet(sheet)

    def test_time_validation(self):
        self.assertEqual(parse_time("8-20"), "08:20")
        for value in ("", "25-00", "8-99", "unknown"):
            with self.assertRaises(SourceError):
                parse_time(value)

    def test_numeric_subject_is_rejected(self):
        sheet = FakeSheet()
        sheet.add_weekly()
        sheet.values[5, 4] = 123
        with self.assertRaises(SourceError):
            parse_sheet(sheet)

    def test_invalid_file_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.xls"
            with self.assertRaises(SourceError):
                read_xls(path)
            path.write_bytes(b"not Excel")
            with self.assertRaises(SourceError):
                read_xls(path)


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "bot.sqlite3")
        await self.database.initialize()
        self.path = Path(self.temp.name) / "schedule.xls"
        self.path.write_bytes(b"source version")
        self.lesson = Lesson("141", 1, 0, 1, "Предмет", "08:20", "09:40")
        checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("tgbot.xls_source.read_xls", return_value=([self.lesson], checksum)):
            await update_source(self.database, self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_failure_preserves_lessons_success_time_and_revision(self):
        before, lessons = await self.database.get_schedule()
        with self.assertRaises(SourceError):
            await update_source(self.database, Path(self.temp.name) / "missing.xls")
        after, saved = await self.database.get_schedule()
        self.assertEqual(saved, lessons)
        self.assertEqual(after["imported_at"], before["imported_at"])
        self.assertTrue(after["last_error"])
        self.assertIn("застарілими", freshness_text(after))
        await self.database.initialize()
        self.assertTrue((await self.database.get_schedule())[0]["last_error"])

    async def test_bad_format_does_not_replace_active_schedule(self):
        with self.assertRaises(SourceError):
            await update_source(self.database, self.path)
        self.assertEqual((await self.database.get_schedule())[1], [self.lesson])

    async def test_conflicting_ons_is_rejected_without_replacing_schedule(self):
        lesson = Lesson("141", 1, 4, 1, "Основи національного спротиву", "08:20", "09:40", week=2)
        with patch("tgbot.xls_source.read_xls", return_value=([lesson], "checksum")):
            with self.assertRaisesRegex(SourceError, "суперечать"):
                await update_source(self.database, self.path)
        self.assertEqual((await self.database.get_schedule())[1], [self.lesson])

    async def test_source_disappearance_and_change_mark_stale(self):
        await check_source(self.database)
        self.assertFalse((await self.database.get_schedule())[0]["last_error"])
        self.path.write_bytes(b"changed source")
        await check_source(self.database)
        self.assertIn("змінився", (await self.database.get_schedule())[0]["last_error"])
        self.path.unlink()
        await check_source(self.database)
        self.assertIn("недоступний", (await self.database.get_schedule())[0]["last_error"])

    async def test_success_clears_stale_state(self):
        await self.database.mark_source_failure("source unavailable", str(self.path))
        checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("tgbot.xls_source.read_xls", return_value=([self.lesson], checksum)):
            await update_source(self.database, self.path)
        status = await self.database.get_source_status()
        self.assertFalse(status["last_error"])
        self.assertEqual(status["source_path"], str(self.path.resolve()))
        self.assertTrue(status["imported_at"])

    async def test_old_source_check_cannot_mark_new_source_stale(self):
        before, _ = await self.database.get_schedule()
        await self.database.replace_schedule("new source", False, [self.lesson], source_path="new.xls", source_hash="new hash")
        await self.database.mark_source_failure("Old source disappeared", str(self.path), expected_source=(before["source_path"], before["source_hash"]))
        self.assertFalse((await self.database.get_schedule())[0]["last_error"])

    async def test_failure_before_first_success_is_visible(self):
        empty = Database(Path(self.temp.name) / "empty.sqlite3")
        await empty.initialize()
        with self.assertRaises(SourceError):
            await update_source(empty, Path(self.temp.name) / "missing.xls")
        status = await empty.get_source_status()
        self.assertIsNone(status["imported_at"])
        self.assertIn("застарілими", freshness_text(status))
