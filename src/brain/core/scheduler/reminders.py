"""Разбор «напомни завтра в 10» в конкретный момент времени.

Модель здесь не вызывается — правило 1 экономики токенов: не звать LLM там,
где хватает кода. Разбор времени детерминирован, проверяется тестами и стоит
ноль, а модель на этом месте стоила бы вызова на каждое сообщение и ошибалась
бы непредсказуемо.

Взамен парсер понимает ровно те формулировки, которыми пользуются вслух.
Не понял — напоминание не создаётся, запись просто сохраняется как мысль:
молча угадать не то время хуже, чем не угадать вовсе.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from brain.core.ports import CaptureUnitOfWork, ReminderDraft
from brain.core.scheduler.queue import REMINDER

# Признак того, что запись вообще про напоминание. Без него любая фраза
# со словом «завтра» превращалась бы в будильник.
TRIGGER = re.compile(r"^\s*(напомни(?:ть)?|напомните)\b[\s,:-]*", re.IGNORECASE)

# Час по умолчанию, если день назван, а время — нет: «напомни завтра».
DEFAULT_HOUR = 10

WEEKDAYS: dict[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6,
}

MONTHS: dict[str, int] = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

# Числительные словами. Вслух говорят именно так: «через две минуты», а не
# «через 2 минуты» — и расшифровка передаёт это словом. Пока их здесь не было,
# голосовые напоминания молча не работали, а тесты этого не видели, потому что
# в них подставляли цифру (16 августа 2026).
WORD_NUMERALS: dict[str, int] = {
    "один": 1,
    "одну": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "пару": 2,
    "пары": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "пятнадцать": 15,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
}

# Длинные варианты вперёд, иначе «двадцать» съест «два».
_NUMERAL_ALTERNATION = "|".join(sorted(WORD_NUMERALS, key=len, reverse=True))

UNIT = r"минут\w*|час\w*|дн\w*|день|недел\w*"

# «через 20 минут», «через час», «через две минуты», «через пару часов»
RELATIVE = re.compile(
    rf"через\s+(?P<count>\d+|{_NUMERAL_ALTERNATION})?\s*(?P<unit>{UNIT})",
    re.IGNORECASE,
)

# «через полчаса», «через пол часа», «через полтора часа».
# Отдельно от остальных: это не «число плюс единица», а готовый отрезок.
FRACTIONAL = re.compile(r"через\s+(?P<what>пол\s?часа|полтора\s+часа)", re.IGNORECASE)
HALF_HOUR = timedelta(minutes=30)
HOUR_AND_A_HALF = timedelta(minutes=90)
# «в 10», «в 10:30», «в 9.15»
CLOCK = re.compile(r"\bв\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\b", re.IGNORECASE)
# «15 сентября»
DATE = re.compile(r"\b(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")\b", re.IGNORECASE)

UNIT_TO_DELTA: dict[str, timedelta] = {
    "минут": timedelta(minutes=1),
    "час": timedelta(hours=1),
    "дн": timedelta(days=1),
    "день": timedelta(days=1),
    "недел": timedelta(weeks=1),
}


@dataclass(frozen=True, slots=True)
class ParsedReminder:
    """Когда напомнить и о чём."""

    fire_at: datetime
    text: str


def _unit_delta(unit: str) -> timedelta | None:
    for prefix, delta in UNIT_TO_DELTA.items():
        if unit.lower().startswith(prefix):
            return delta
    return None


def _clean(text: str, *matched: re.Match[str] | None) -> str:
    """Убрать из текста служебные куски, оставив суть напоминания."""
    result = TRIGGER.sub("", text)
    for match in matched:
        if match is not None:
            result = result.replace(match.group(0), " ")
    # Предлоги, оставшиеся без своего времени: «напомни в про отчёт».
    result = re.sub(r"\s+", " ", result).strip(" ,.;:-")
    return result or text.strip()


def _count_of(spoken: str | None) -> int:
    """«3» и «три» — одно и то же число. Пусто («через час») — единица."""
    if not spoken:
        return 1
    if spoken.isdigit():
        return int(spoken)
    return WORD_NUMERALS[spoken.lower()]


def _relative(text: str, now: datetime) -> tuple[datetime, re.Match[str]] | None:
    if fractional := FRACTIONAL.search(text):
        whole = re.sub(r"\s+", " ", fractional.group("what").lower())
        return now + (HOUR_AND_A_HALF if whole.startswith("полтора") else HALF_HOUR), fractional

    match = RELATIVE.search(text)
    if match is None:
        return None

    unit = _unit_delta(match.group("unit"))
    if unit is None:  # pragma: no cover — регулярка не пропустит чужую единицу
        return None

    return now + unit * _count_of(match.group("count")), match


def _absolute(text: str, now: datetime) -> tuple[datetime, tuple[re.Match[str] | None, ...]] | None:
    """Разобрать «завтра/в понедельник/15 сентября» плюс необязательное время."""
    day = _named_day(text, now)
    clock = CLOCK.search(text)

    if day is None and clock is None:
        return None

    date_match, target = day if day is not None else (None, now)

    hour, minute = DEFAULT_HOUR, 0
    if clock is not None:
        hour = int(clock.group("hour"))
        minute = int(clock.group("minute") or 0)
        if hour > 23 or minute > 59:
            return None

    fire_at = target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # «напомни в 10», сказанное в одиннадцать, — это про завтра.
    # Без этого напоминание молча уезжало бы в прошлое и не срабатывало.
    if fire_at <= now and date_match is None:
        fire_at += timedelta(days=1)

    return fire_at, (date_match, clock)


def _named_day(text: str, now: datetime) -> tuple[re.Match[str] | None, datetime] | None:
    """День, названный словом или датой."""
    if match := re.search(r"\bпослезавтра\b", text, re.IGNORECASE):
        return match, now + timedelta(days=2)
    if match := re.search(r"\bзавтра\b", text, re.IGNORECASE):
        return match, now + timedelta(days=1)
    if match := re.search(r"\bсегодня\b", text, re.IGNORECASE):
        return match, now

    if match := re.search(r"\bв\s+(" + "|".join(WEEKDAYS) + r")\b", text, re.IGNORECASE):
        target_weekday = WEEKDAYS[match.group(1).lower()]
        ahead = (target_weekday - now.weekday()) % 7 or 7
        return match, now + timedelta(days=ahead)

    if match := DATE.search(text):
        month = MONTHS[match.group("month").lower()]
        day_number = int(match.group("day"))
        year = now.year + (1 if (month, day_number) < (now.month, now.day) else 0)
        try:
            return match, now.replace(
                year=year, month=month, day=day_number, hour=0, minute=0, second=0, microsecond=0
            )
        except ValueError:  # 31 февраля
            return None

    return None


def parse(text: str, *, now: datetime, timezone: str) -> ParsedReminder | None:
    """Разобрать напоминание. `None` — если это обычная мысль, а не будильник.

    `now` передаётся снаружи, а не берётся из часов: иначе тест на «завтра
    в 10» пришлось бы гонять, глядя на календарь.
    """
    if not TRIGGER.match(text):
        return None

    local_now = now.astimezone(ZoneInfo(timezone))

    if relative := _relative(text, local_now):
        fire_at, match = relative
        return ParsedReminder(fire_at=fire_at, text=_clean(text, match))

    if absolute := _absolute(text, local_now):
        fire_at, matches = absolute
        return ParsedReminder(fire_at=fire_at, text=_clean(text, *matches))

    return None


NOT_UNDERSTOOD = (
    "Не понял, когда напомнить — записал как обычную мысль.\n"
    "Скажите точнее: «через 10 минут», «завтра в 10», «в понедельник»."
)


@dataclass(frozen=True, slots=True)
class ScheduleOutcome:
    """Что случилось с напоминанием и что об этом сказать владельцу."""

    message: str
    created: bool


async def schedule(
    uow: CaptureUnitOfWork,
    *,
    entry_id: int,
    chat_id: int,
    text: str,
    timezone: str,
    now: datetime,
) -> ScheduleOutcome | None:
    """Завести напоминание, если текст им является. `None` — если не является.

    Одна функция на оба канала: текст разбирается при приёме, голос — после
    расшифровки, в воркере.

    Три исхода, и третий — главный. Если слово «напомни» прозвучало, а время
    не разобрано, система **говорит об этом вслух**. Молчаливое сохранение
    здесь хуже отказа: владелец уверен, что напоминание заведено, и узнаёт
    обратное, не дождавшись его. Именно так 16 августа пропали и «через две
    минуты», и «через 3 минуты» — обе записи спокойно легли в архив.

    Напоминание и задание пишутся одной транзакцией — инвариант 7 касается
    не только записей.
    """
    if not TRIGGER.match(text):
        return None

    parsed = parse(text, now=now, timezone=timezone)
    if parsed is None:
        return ScheduleOutcome(message=NOT_UNDERSTOOD, created=False)

    reminder_id = await uow.add_reminder(
        ReminderDraft(source_entry_id=entry_id, fire_at=parsed.fire_at, text=parsed.text)
    )
    await uow.enqueue(
        REMINDER,
        {"reminder_id": reminder_id, "chat_id": chat_id},
        execute_after=parsed.fire_at - now,
    )
    return ScheduleOutcome(
        message=f"Напомню {parsed.fire_at:%d.%m в %H:%M}.",
        created=True,
    )
