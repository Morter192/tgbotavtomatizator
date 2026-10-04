import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from tgbot.schedule import KYIV, Lesson, ONS, format_lesson, lessons_for_day, next_lesson, read_schedule, week_type


class CalendarTests(unittest.TestCase):
    def test_weeks_change_on_monday_from_anchor(self):
        for day, expected in (("2026-09-01", 1), ("2026-10-04", 1), ("2026-10-05", 2),
                              ("2026-10-11", 2), ("2026-10-12", 1), ("2026-10-19", 2)):
            self.assertEqual(week_type(date.fromisoformat(day)), expected)

    def test_next_skips_started_class_and_finds_next_cycle(self):
        lessons = [Lesson("141", 1, 0, 1, "Парна", "08:20", "09:40", week=2)]
        result = next_lesson(lessons, datetime(2026, 10, 5, 8, 20, tzinfo=KYIV), True)
        self.assertEqual(result[0], date(2026, 10, 19))

    def test_next_finds_far_future_start_and_excludes_expired(self):
        lesson = Lesson("141", 1, 0, 1, "Майбутня", "08:20", "09:40", valid_from="2027-01-01", valid_to="2027-01-04")
        result = next_lesson([lesson], datetime(2026, 10, 5, tzinfo=KYIV), True)
        self.assertEqual(result[0], date(2027, 1, 4))
        self.assertIsNone(next_lesson([lesson], datetime(2027, 1, 5, tzinfo=KYIV), True))

    def test_date_bounds_are_inclusive(self):
        lesson = Lesson("141", 1, 4, 4, "Предмет", "13:00", "14:20", valid_from="2026-10-09", valid_to="2026-10-16")
        self.assertTrue(lesson.occurs(date(2026, 10, 9)))
        self.assertTrue(lesson.occurs(date(2026, 10, 16)))
        self.assertFalse(lesson.occurs(date(2026, 10, 23)))

    def test_ons_replaces_legacy_and_does_not_cancel_other_subjects(self):
        lessons = [Lesson("141", 1, 4, 1, ONS, "08:20", "09:40", week=1),
                   Lesson("141", 1, 4, 4, "Інший предмет", "13:00", "14:20")]
        actual = lessons_for_day(lessons, date(2026, 10, 16), False)
        ons = [lesson for lesson in actual if lesson.subject == ONS]
        self.assertEqual([lesson.number for lesson in ons], [4, 5, 6])
        self.assertTrue(all(lesson.online and lesson.time_confirmed for lesson in ons))
        self.assertEqual([lesson.start for lesson in ons], ["13:00", "14:40", "16:10"])
        self.assertNotIn("потребує підтвердження", format_lesson(ons[0]))
        self.assertIn("Інший предмет", [lesson.subject for lesson in actual])
        for day in (date(2026, 10, 9), date(2026, 10, 23), date(2026, 10, 30)):
            self.assertFalse(any(lesson.subject == ONS for lesson in lessons_for_day(lessons, day, False)))

    def test_dst_and_online_missing_location(self):
        self.assertEqual(datetime(2026, 10, 23, 13, tzinfo=KYIV).utcoffset().total_seconds(), 10800)
        self.assertEqual(datetime(2026, 10, 26, 13, tzinfo=KYIV).utcoffset().total_seconds(), 7200)
        self.assertIn("посилання не вказано", format_lesson(Lesson("141", 1, 0, 1, "Онлайн", "08:20", "09:40", online=True)))
        self.assertIn("не вказано", format_lesson(Lesson("141", 1, 0, 1, "Без місця", "08:20", "09:40")))

    def test_invalid_lessons_rejected(self):
        for kwargs in ({"start": "8:20"}, {"end": "07:00"}, {"week": 3}, {"weekday": True}, {"subgroup": 0}):
            fields = dict(group="141", subgroup=1, weekday=0, number=1, subject="Предмет", start="08:20", end="09:40")
            fields.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Lesson(**fields)

    def test_demo_file_valid_and_explicit(self):
        source, is_demo, lessons = read_schedule(Path(__file__).resolve().parent.parent / "examples/demo_schedule.json")
        self.assertTrue(is_demo)
        self.assertIn("Тестовий", source)
        self.assertEqual(len(lessons), 4)

    def test_invalid_json_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"source": "demo", "is_demo": "false", "lessons": []}))
            with self.assertRaises(ValueError):
                read_schedule(path)
