"""Модель, календар і відображення розкладу."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
EVEN_MONDAY = date(2026, 10, 5)
DAYS = ("Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця", "Субота", "Неділя")
ONS = "Основи національного спротиву"


def week_type(day: date) -> int:
    monday = day - timedelta(days=day.weekday())
    return 2 if ((monday - EVEN_MONDAY).days // 7) % 2 == 0 else 1


def week_label(day: date) -> str:
    return "парний, 2-й" if week_type(day) == 2 else "непарний, 1-й"


@dataclass(frozen=True)
class Lesson:
    group: str
    subgroup: int | None
    weekday: int  # 0 — понеділок
    number: int
    subject: str
    start: str
    end: str
    week: int = 0  # 0 — щотижня, 1 — непарний, 2 — парний
    teacher: str = ""
    building: str = ""
    room: str = ""
    location: str = ""
    online: bool = False
    url: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    time_confirmed: bool = True
    source_cells: str = ""
    cancelled_dates: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("group", "subject", "start", "end", "teacher", "building", "room", "location", "url", "source_cells"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError(f"Некоректне поле заняття: {name}.")
        if not self.group.strip() or not self.subject.strip():
            raise ValueError("Група та предмет не можуть бути порожніми.")
        for name in ("weekday", "number", "week"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"Поле {name} має бути цілим числом.")
        if self.subgroup is not None and (type(self.subgroup) is not int or self.subgroup < 1):
            raise ValueError("Підгрупа має бути додатним числом або null для спільних занять.")
        if not 0 <= self.weekday <= 6 or not 1 <= self.number <= 7 or self.week not in (0, 1, 2):
            raise ValueError("Некоректний день, номер пари або тип тижня.")
        for name in ("online", "time_confirmed"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Поле {name} має бути true або false.")
        for clock in (self.start, self.end):
            parsed = time.fromisoformat(clock)
            if parsed.tzinfo is not None or parsed.strftime("%H:%M") != clock:
                raise ValueError("Час заняття має бути у форматі HH:MM.")
        if self.end <= self.start:
            raise ValueError("Завершення пари має бути після початку.")
        for value in (self.valid_from, self.valid_to):
            if value is not None and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Дата має бути у форматі YYYY-MM-DD.")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("Кінець періоду має бути не раніше початку.")
        if not isinstance(self.cancelled_dates, (list, tuple)) or len(self.cancelled_dates) > 100:
            raise ValueError("cancelled_dates має містити до 100 дат скасування.")
        for value in self.cancelled_dates:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Дата скасування має бути у форматі YYYY-MM-DD.")
        object.__setattr__(self, "cancelled_dates", tuple(sorted(set(self.cancelled_dates))))

    def occurs(self, day: date) -> bool:
        return (
            day.weekday() == self.weekday
            and self.week in (0, week_type(day))
            and (self.valid_from is None or day >= date.fromisoformat(self.valid_from))
            and (self.valid_to is None or day <= date.fromisoformat(self.valid_to))
            and day.isoformat() not in self.cancelled_dates
        )


def read_schedule(path: Path) -> tuple[str, bool, list[Lesson]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"source", "is_demo", "lessons"}:
        raise ValueError("JSON має містити source, is_demo та lessons.")
    if not isinstance(data["source"], str) or not data["source"].strip() or len(data["source"]) > 500:
        raise ValueError("Вкажіть назву джерела розкладу.")
    if type(data["is_demo"]) is not bool or not isinstance(data["lessons"], list):
        raise ValueError("is_demo має бути true/false, lessons — список занять.")
    try:
        lessons = [Lesson(**item) for item in data["lessons"]]
    except (TypeError, ValueError) as error:
        raise ValueError(f"Некоректне заняття у JSON: {error}") from None
    if len({json.dumps(asdict(lesson), sort_keys=True) for lesson in lessons}) != len(lessons):
        raise ValueError("У розкладі є дублікати занять.")
    return data["source"], data["is_demo"], lessons


def lessons_for_day(lessons: list[Lesson], day: date, is_demo: bool) -> list[Lesson]:
    # Особисте правило ОНС не змінює демонстраційні дані.
    result = [lesson for lesson in lessons if lesson.occurs(day) and (is_demo or lesson.subject.strip() != ONS)]
    ons_cancelled = any(lesson.subject.strip() == ONS and day.isoformat() in lesson.cancelled_dates for lesson in lessons)
    if not is_demo and not ons_cancelled and date(2026, 10, 5) <= day <= date(2026, 10, 23) and day.weekday() == 4 and week_type(day) == 1:
        for number, start, end in ((4, "13:00", "14:20"), (5, "14:40", "16:00"), (6, "16:10", "17:30")):
            result.append(Lesson("141", 1, 4, number, ONS, start, end, week=1, online=True, time_confirmed=True))
    return sorted(result, key=lambda lesson: (lesson.start, lesson.number, lesson.subject))


def next_lesson(lessons: list[Lesson], now: datetime, is_demo: bool) -> tuple[date, Lesson] | None:
    if now.tzinfo is None:
        raise ValueError("Потрібен час із часовим поясом.")
    now = now.astimezone(KYIV)
    candidates: list[tuple[datetime, Lesson]] = []
    # Для кожного заняття достатньо одного повного двотижневого циклу
    # після початку його дії, навіть якщо початок далеко в майбутньому.
    days = set()
    for lesson in lessons:
        first = max(now.date(), date.fromisoformat(lesson.valid_from) if lesson.valid_from else now.date())
        days.update(first + timedelta(days=offset) for offset in range(15 + 14 * len(lesson.cancelled_dates)))
    if not is_demo and now.date() <= date(2026, 10, 16):
        days.add(date(2026, 10, 16))
    for day in days:
        for lesson in lessons_for_day(lessons, day, is_demo):
            starts_at = datetime.combine(day, time.fromisoformat(lesson.start), KYIV)
            if starts_at > now:
                candidates.append((starts_at, lesson))
    if not candidates:
        return None
    starts_at, lesson = min(candidates, key=lambda item: (item[0], item[1].number, item[1].subject))
    return starts_at.date(), lesson


def format_lesson(lesson: Lesson) -> str:
    lines = [f"{lesson.number}-та пара · {lesson.start}–{lesson.end}", lesson.subject]
    if lesson.online:
        lines.append(f"Онлайн · {lesson.url or 'посилання не вказано'}")
    elif lesson.building or lesson.room:
        if lesson.building:
            lines.append(f"Корпус: {lesson.building}")
        if lesson.room:
            lines.append(f"Аудиторія: {lesson.room}")
        if lesson.location:
            lines.append(f"Місце: {lesson.location}")
    else:
        lines.append(f"Місце: {lesson.location or 'не вказано'}")
    if lesson.teacher:
        lines.append(f"Викладач: {lesson.teacher}")
    if not lesson.time_confirmed:
        lines.append("Час потребує підтвердження; нагадування не активовано.")
    return "\n".join(lines)


def format_day(day: date, lessons: list[Lesson]) -> str:
    title = f"{DAYS[day.weekday()]}, {day:%d.%m.%Y} · {week_label(day)} тиждень"
    conflicts = any(a.start < b.end and b.start < a.end for index, a in enumerate(lessons) for b in lessons[index + 1:])
    warning = "\nУвага: у розкладі є заняття, що накладаються за часом." if conflicts else ""
    return title + warning + "\n\n" + ("\n\n".join(map(format_lesson, lessons)) if lessons else "Цього дня пар немає.")
