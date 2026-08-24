"""Разбор «напомни завтра в 10» в конкретный момент времени.

Модель здесь не вызывается — правило 1 экономики токенов: не звать LLM там,
где хватает кода. Разбор времени детерминирован, проверяется тестами и стоит
ноль, а модель на этом месте стоила бы вызова на каждое сообщение и ошибалась
бы непредсказуемо.

Взамен парсер понимает ровно те формулировки, которыми пользуются вслух.
Не понял — напоминание не создаётся, запись просто сохраняется как мысль:
молча угадать не то время хуже, чем не угадать вовсе.
"""

import enum
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from brain.core.ports import CaptureUnitOfWork, ReminderDraft
from brain.core.scheduler.queue import REMINDER

# Признак того, что запись вообще про напоминание. Без него любая фраза
# со словом «завтра» превращалась бы в будильник.
#
# Цепляемся за корень, а не за конкретную форму, и ищем по всей фразе,
# а не только в начале. Живая речь не выдаёт «напомни …» первым словом:
# 18 августа расшифровка записи 26 отдала глагол в прошедшем времени, триггер
# в середине фразы и оборванное слово между ним и временем — якорь `^` вместе
# с формой `напомни\b` отсекли это дважды.
#
# Цена расширения — обычная мысль со словом «напоминание» может стать
# будильником. Она принята сознательно: во-первых, во всём архиве на день
# правки таких мыслей нет ни одной, во-вторых, время всё равно должно
# разобраться, иначе бот скажет «не понял» вслух и ничего не заведёт.
TRIGGER = re.compile(r"\bнапом(?:н|ина)\w*", re.IGNORECASE)

# Час по умолчанию, если день назван, а время — нет: «напомни завтра».
DEFAULT_HOUR = 10

NOON = 12
MIDNIGHT = 0
# «в час дня» — это один час, а не слово «час» как единица измерения.
HOUR_WORD = 1
# «в конце дня» — конец рабочего дня, а не полночь.
END_OF_DAY_HOUR = 18

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

# Время суток. Вслух почти никогда не говорят «в 19:40» — говорят
# «в 7:40 вечера», и без этого словаря вечер уезжал в утро на двенадцать часов.
MORNING, AFTERNOON, EVENING, NIGHT = "утро", "день", "вечер", "ночь"

DAYPARTS: dict[str, str] = {
    "утра": MORNING,
    "утром": MORNING,
    "дня": AFTERNOON,
    "днём": AFTERNOON,
    "днем": AFTERNOON,
    "вечера": EVENING,
    "вечером": EVENING,
    "ночи": NIGHT,
    "ночью": NIGHT,
}

# Час, если время суток названо, а часы — нет: «в среду вечером».
DAYPART_HOUR: dict[str, int] = {MORNING: 9, AFTERNOON: 14, EVENING: 19, NIGHT: 23}

_DAYPART_ALTERNATION = "|".join(sorted(DAYPARTS, key=len, reverse=True))

# Предлог перед часами. «до» нужен ради «за 10 минут до 15:00»: там время
# названо без «в», и без этого варианта формулировка не разбиралась вовсе.
PREPOSITION = r"(?:в|до|к)"

# «в 10», «в 10:30», «в 9.15», «в 7:40 вечера», «в 9 часов вечера», «в час дня».
# `час\b` — именно с границей: иначе «в частности» читалось бы как час ночи.
CLOCK = re.compile(
    rf"\b{PREPOSITION}\s+(?P<hour>\d{{1,2}}|час\b)(?:[:.](?P<minute>\d{{2}}))?(?!\d)"
    r"(?:\s*час\w*)?"
    rf"(?:\s+(?P<daypart>{_DAYPART_ALTERNATION}))?",
    re.IGNORECASE,
)

# Время суток, названное отдельно от часов: «вечером в 20:00», «в среду вечером».
DAYPART = re.compile(rf"\b(?P<daypart>{_DAYPART_ALTERNATION})\b", re.IGNORECASE)

# «в полдень», «в полночь» — часов не называют вовсе.
NOON_MIDNIGHT = re.compile(rf"\b{PREPOSITION}\s+(?P<what>полдень|полночь)\b", re.IGNORECASE)

# «в конце дня». Проверяется раньше остальных: внутри сидит «дня»,
# которое иначе прочиталось бы как время суток.
END_OF_DAY = re.compile(r"\bв\s+конце\s+дня\b", re.IGNORECASE)

# «за 10 минут до 15:00», «за минут 10 до этого» — оба порядка слов живые.
# Предлог «до» намеренно остаётся снаружи совпадения: его забирает `CLOCK`,
# и без этого два куска перекрывались бы при вычистке текста.
LEAD = re.compile(
    rf"за\s+(?:(?P<count>\d+|{_NUMERAL_ALTERNATION})\s*(?P<unit>{UNIT})"
    rf"|(?P<unit_first>{UNIT})\s+(?P<count_last>\d+|{_NUMERAL_ALTERNATION}))"
    r"(?=\s+до\b)",
    re.IGNORECASE,
)
LEAD_FRACTIONAL = re.compile(r"за\s+(?P<what>пол\s?часа|полтора\s+часа)(?=\s+до\b)", re.IGNORECASE)

# «15 сентября»
DATE = re.compile(r"\b(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")\b", re.IGNORECASE)

# Слова-паразиты расшифровки: «э», «э-э», «мм». Живая речь ими набита,
# а в тексте напоминания им делать нечего.
INTERJECTION = re.compile(r"\b(?:э+(?:[\s-]*э+)*|м{2,})\b[\s,]*", re.IGNORECASE)

# «напомни **мне** уйти» — местоимение сразу за триггером всегда лишнее.
# В базе из-за него лежат «мне посмотреть календарь» и «мне уйти вечера домой».
LEADING_PRONOUN = re.compile(r"^[\s,]*(?:мне|меня)\b[\s,]*", re.IGNORECASE)

# Сколько букв должно остаться после триггера, чтобы считать это сутью.
MIN_MEANINGFUL = 3

# Повелительные глаголы: признак просьбы, когда слова «напомни» не прозвучало.
#
# Так появилась запись 27 — расшифровка съела первую букву у «Напомни»,
# осталось «помни …», и запись легла в архив молча. В триггер
# «помни» не идёт намеренно: «помни, что завтра встреча» — мысль, а не
# будильник. Вместо этого система спрашивает.
#
# Список явный, а не морфологическое правило. Повелительное наклонение
# по-русски кончается на -и/-й/-ь, и под то же правило попадают «пятнадцати»,
# «мой», «здесь», «теперь» — стоп-лист рос бы от каждого ложного вопроса.
# Разобрать наклонение честно умеет только морфологическая библиотека, а это
# новая зависимость и отдельный ADR.
#
# Цена выбора названа вслух: **незнакомый глагол пропускается молча.**
# Список пополняется одной строкой, и это ручная работа владельца.
# На архиве из 28 записей он срабатывает ровно один раз — на записи 27.
IMPERATIVE_VERBS = (
    "помни",
    "запомни",
    "забудь",
    "разбуди",
    "предупреди",
    "напиши",
    "скажи",
    "позвони",
    "поставь",
    "заведи",
    "купи",
    "забери",
    "проверь",
    "отправь",
    "собери",
    "сходи",
    "заплати",
    "оплати",
    "возьми",
    "вынеси",
    "выйди",
    "уйди",
)

# «не» подхватывается вместе с глаголом: «не забудь позвонить» — одна просьба,
# и частица не должна остаться в тексте напоминания.
IMPERATIVE = re.compile(
    r"\b(?:не\s+)?(?:" + "|".join(IMPERATIVE_VERBS) + r")\b[\s,:-]*", re.IGNORECASE
)

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


def _squeeze(text: str) -> str:
    """Схлопнуть пробелы и обрезать пунктуацию, оставшуюся от вырезанных кусков."""
    return re.sub(r"\s+", " ", text).strip(" ,.;:-—")


def _clean(text: str, marker: re.Match[str] | None, *matched: re.Match[str] | None) -> str:
    """Убрать из текста служебные куски, оставив суть напоминания.

    Суть ищется после служебного слова: «напомни **уйти с офиса** в 19:40».
    Если после него ничего не осталось («уйти с офиса, напомни»), берётся то,
    что перед. Пустой `marker` — у ответа на вопрос («через час») служебного
    слова нет вовсе, и вырезать из него нечего.
    """
    if marker is None:
        result = text
    else:
        after = text[marker.end() :]
        result = after if len(_squeeze(after)) >= MIN_MEANINGFUL else text[: marker.start()]

    for match in matched:
        if match is not None:
            result = result.replace(match.group(0), " ")

    result = INTERJECTION.sub(" ", result)
    result = LEADING_PRONOUN.sub("", result)
    return _squeeze(result) or text.strip()


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


class _ImpossibleTimeError(Exception):
    """Часы названы, но таких часов не бывает: «в 99».

    Отдельно от «времени в тексте нет»: там разбор продолжается и день может
    взять час по умолчанию, а здесь продолжать нельзя. «Напомни завтра в 99»
    обязано остаться непонятым, а не тихо стать десятью утра.
    """


@dataclass(frozen=True, slots=True)
class _TimeOfDay:
    """Час и минута, названные вслух, и куски текста, из которых они взялись."""

    hour: int
    minute: int
    matches: tuple[re.Match[str] | None, ...]


def _apply_daypart(hour: int, named: str | None) -> int:
    """Перевести двенадцатичасовое время в двадцатичетырёхчасовое.

    Час больше двенадцати уже сказан по-двадцатичетырёхчасовому: «в 19:40
    вечера» — это 19:40, а не 31:40, и слово здесь просто уточнение. Ровно так
    владелец и написал 18 августа, после того как голосовое с «7:40 вечера»
    не сработало.
    """
    if named is None or hour > NOON:
        return hour

    daypart = DAYPARTS[named.lower()]
    if daypart in (MORNING, NIGHT):
        return MIDNIGHT if hour == NOON else hour
    return hour if hour == NOON else hour + NOON


def _time_of_day(text: str) -> _TimeOfDay | None:
    """Время, названное часами или временем суток. `None` — если его нет вовсе."""
    if match := NOON_MIDNIGHT.search(text):
        hour = NOON if match.group("what").lower() == "полдень" else MIDNIGHT
        return _TimeOfDay(hour, 0, (match,))

    if match := END_OF_DAY.search(text):
        return _TimeOfDay(END_OF_DAY_HOUR, 0, (match,))

    clock = CLOCK.search(text)

    # Время суток может стоять как вплотную к часам («в 7 вечера»), так и
    # отдельно от них («вечером в 20:00»). Второе ищется только если первого нет.
    daypart_match = None
    named = clock.group("daypart") if clock is not None else None
    if named is None and (daypart_match := DAYPART.search(text)) is not None:
        named = daypart_match.group("daypart")

    if clock is not None:
        spoken = clock.group("hour").lower()
        hour = HOUR_WORD if spoken == "час" else int(spoken)
        minute = int(clock.group("minute") or 0)
        if hour > 23 or minute > 59:
            raise _ImpossibleTimeError
        return _TimeOfDay(_apply_daypart(hour, named), minute, (clock, daypart_match))

    if named is not None:
        return _TimeOfDay(DAYPART_HOUR[DAYPARTS[named.lower()]], 0, (daypart_match,))

    return None


def _lead(text: str) -> tuple[timedelta, re.Match[str] | None]:
    """Насколько раньше названного времени: «за 10 минут до 15:00» — это 14:50."""
    if match := LEAD_FRACTIONAL.search(text):
        whole = re.sub(r"\s+", " ", match.group("what").lower())
        return (HOUR_AND_A_HALF if whole.startswith("полтора") else HALF_HOUR), match

    match = LEAD.search(text)
    if match is None:
        return timedelta(), None

    unit = _unit_delta(match.group("unit") or match.group("unit_first"))
    if unit is None:  # pragma: no cover — регулярка не пропустит чужую единицу
        return timedelta(), None

    return unit * _count_of(match.group("count") or match.group("count_last")), match


def _absolute(text: str, now: datetime) -> tuple[datetime, tuple[re.Match[str] | None, ...]] | None:
    """Разобрать «завтра/в понедельник/15 сентября» плюс необязательное время."""
    day = _named_day(text, now)
    time_of_day = _time_of_day(text)

    if day is None and time_of_day is None:
        return None

    date_match, target = day if day is not None else (None, now)

    time_matches: tuple[re.Match[str] | None, ...] = ()
    hour, minute = DEFAULT_HOUR, 0
    if time_of_day is not None:
        hour, minute, time_matches = time_of_day.hour, time_of_day.minute, time_of_day.matches

    fire_at = target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Сдвиг назад — до проверки на прошлое: «за 10 минут до 15:00», сказанное
    # без четверти три, это сегодня, а не завтра.
    lead, lead_match = _lead(text)
    fire_at -= lead

    # «напомни в 10», сказанное в одиннадцать, — это про завтра.
    # Без этого напоминание молча уезжало бы в прошлое и не срабатывало.
    if fire_at <= now and date_match is None:
        fire_at += timedelta(days=1)

    return fire_at, (date_match, *time_matches, lead_match)


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
    trigger = TRIGGER.search(text)
    if trigger is None:
        return None
    return _parse_at(text, trigger, now=now, timezone=timezone)


def _parse_at(
    text: str, marker: re.Match[str] | None, *, now: datetime, timezone: str
) -> ParsedReminder | None:
    """Разобрать время, считая `marker` служебным словом, а не сутью.

    Отдельно от `parse`, потому что служебным словом бывает не только
    «напомни»: у записи без триггера ту же роль играет повелительный глагол
    («помни», «не забудь»), а у ответа на вопрос — ничего.
    """
    local_now = now.astimezone(ZoneInfo(timezone))

    if relative := _relative(text, local_now):
        fire_at, match = relative
        return ParsedReminder(fire_at=fire_at, text=_clean(text, marker, match))

    try:
        absolute = _absolute(text, local_now)
    except _ImpossibleTimeError:
        return None

    if absolute is not None:
        fire_at, matches = absolute
        return ParsedReminder(fire_at=fire_at, text=_clean(text, marker, *matches))

    return None


NOT_UNDERSTOOD = (
    "Не понял, когда напомнить — записал как обычную мысль.\n"
    "Скажите точнее: «через 10 минут», «завтра в 10», «в 7 вечера», «в понедельник»."
)


# Вопросы к записи, которая похожа на напоминание, но слова «напомни» в ней нет.
CONFIRM_QUESTION = "Похоже на напоминание: «{what}».\nПоставить на {when:%d.%m в %H:%M}?"
WHEN_QUESTION = (
    "Похоже на напоминание: «{what}».\n"
    "Когда напомнить? Ответьте на это сообщение — «через час», «сегодня в 19:00», «завтра в 10»."
)


class Ask(enum.StrEnum):
    """О чём спросить владельца, если спрашивать вообще есть о чём."""

    NOTHING = "nothing"
    # Время разобрано — остаётся подтвердить кнопкой.
    CONFIRM = "confirm"
    # Времени в записи нет вовсе, и ответ придёт текстом.
    WHEN = "when"


@dataclass(frozen=True, slots=True)
class ScheduleOutcome:
    """Что случилось с напоминанием и что об этом сказать владельцу."""

    message: str
    created: bool
    ask: Ask = Ask.NOTHING


def _question(text: str, *, now: datetime, timezone: str) -> ScheduleOutcome | None:
    """Спросить про запись без «напомни», но с повелительным глаголом.

    `None` — если спрашивать не о чем: обычной мысли вопросы не задают.
    Восемь записей архива содержат время без всякой просьбы («у меня экзамен
    в 9:00», «у нас матч в 7:00»), и вопрос к каждой обесценил бы вопрос там,
    где он нужен.

    Напоминание здесь не заводится ни в одной ветке. Вопрос — это вопрос:
    угадать за владельца и завести молча значит вернуться к тому, что чиним.
    """
    imperative = IMPERATIVE.search(text)
    if imperative is None:
        return None

    parsed = _parse_at(text, imperative, now=now, timezone=timezone)
    if parsed is not None:
        return ScheduleOutcome(
            message=CONFIRM_QUESTION.format(what=parsed.text, when=parsed.fire_at),
            created=False,
            ask=Ask.CONFIRM,
        )

    # Времени нет — но просьба очевидна. Промолчать здесь значит воспроизвести
    # ровно ту дыру, ради которой всё это заведено: «не забудь позвонить маме»
    # тихо легло бы в архив.
    return ScheduleOutcome(
        message=WHEN_QUESTION.format(what=_clean(text, imperative)),
        created=False,
        ask=Ask.WHEN,
    )


def promise(fire_at: datetime) -> str:
    """Обещание, которое видит владелец. Одна формулировка на два места.

    Второе место — перерисовка подтверждения приёма после чистки: сообщение
    собирается заново, и строка обязана получиться той же самой, иначе
    вычищенный текст пришёл бы вместе с чуть другим обещанием.
    """
    return f"Напомню {fire_at:%d.%m в %H:%M}."


def shown(
    text: str, *, fire_at: datetime | None, now: datetime, timezone: str
) -> ScheduleOutcome | None:
    """Что владельцу уже сказали про напоминание — собранное заново.

    Нужна перерисовке подтверждения приёма. Подтверждение живёт в Telegram,
    прочитать его нельзя — только заменить целиком; значит, всё, что там было,
    надо уметь восстановить. Тот же приём, что в `enrich/reply.py`.

    Восстанавливать приходится **три разных сообщения**, а не одно: обещание
    («⏰ Напомню в 19:40»), вопрос с кнопками («❓ Похоже на напоминание…»)
    и признание, что время не разобрано. Потерять любое из них при перерисовке
    значит соврать: первое читается как «напоминание не завелось», второе
    уносит с собой единственный способ ответить.

    `fire_at` приходит из базы, а не пересчитывается из текста, и это главное
    здесь. «Через 3 минуты», разобранное заново через полминуты, дало бы время
    на полминуты позже настоящего — обещание, которого система не давала.
    """
    if fire_at is not None:
        return ScheduleOutcome(
            message=promise(fire_at.astimezone(ZoneInfo(timezone))), created=True
        )

    if not TRIGGER.search(text):
        return _question(text, now=now, timezone=timezone)

    # Слово «напомни» прозвучало, напоминания нет. Значит, время не разобрали,
    # и об этом было сказано вслух — молчаливое сохранение здесь хуже отказа.
    return ScheduleOutcome(message=NOT_UNDERSTOOD, created=False)


async def _create(
    uow: CaptureUnitOfWork,
    *,
    entry_id: int,
    chat_id: int,
    parsed: ParsedReminder,
    now: datetime,
) -> ScheduleOutcome:
    """Записать напоминание и задание — одной транзакцией (инвариант 7)."""
    reminder_id = await uow.add_reminder(
        ReminderDraft(source_entry_id=entry_id, fire_at=parsed.fire_at, text=parsed.text)
    )
    await uow.enqueue(
        REMINDER,
        {"reminder_id": reminder_id, "chat_id": chat_id},
        execute_after=parsed.fire_at - now,
    )
    return ScheduleOutcome(message=promise(parsed.fire_at), created=True)


async def schedule_confirmed(
    uow: CaptureUnitOfWork,
    *,
    entry_id: int,
    chat_id: int,
    subject: str,
    when: str,
    timezone: str,
    now: datetime,
) -> ScheduleOutcome:
    """Завести напоминание по ответу владельца на вопрос.

    Два случая, и оба сюда: нажатое «да» — тогда `when` это сам текст записи,
    и ответ текстом («сегодня в 19:00») — тогда `when` это ответ, а суть
    по-прежнему берётся из записи.

    Время разбирается заново, от текущего момента: между вопросом и ответом
    проходит неизвестно сколько, и «в 19:40», уже наступившее, должно уехать
    на завтра, а не в прошлое.
    """
    parsed = _parse_at(
        when,
        IMPERATIVE.search(when) or TRIGGER.search(when),
        now=now,
        timezone=timezone,
    )
    if parsed is None:
        return ScheduleOutcome(message=NOT_UNDERSTOOD, created=False)

    # Нажатое «да»: время лежало внутри самой записи, и `parsed.text` — это уже
    # её суть без времени. Ответ текстом: время пришло отдельной фразой,
    # суть надо достать из записи самостоятельно.
    what = parsed.text if when == subject else _subject_of(subject)
    return await _create(
        uow,
        entry_id=entry_id,
        chat_id=chat_id,
        parsed=ParsedReminder(fire_at=parsed.fire_at, text=what),
        now=now,
    )


def _subject_of(text: str) -> str:
    """Суть записи без повелительного глагола: «не забудь позвонить маме»."""
    imperative = IMPERATIVE.search(text)
    return _clean(text, imperative) if imperative is not None else _squeeze(text)


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

    Слова «напомни» может не быть вовсе — тогда решает повелительный глагол,
    и система **спрашивает**, а не заводит и не молчит. Так закрыт последний
    способ тихо потерять просьбу: запись 27, у которой расшифровка съела первую
    букву у «Напомни», легла в архив без единого слова — триггер за «помни»
    не цепляется намеренно.

    Напоминание и задание пишутся одной транзакцией — инвариант 7 касается
    не только записей.
    """
    if not TRIGGER.search(text):
        return _question(text, now=now, timezone=timezone)

    parsed = parse(text, now=now, timezone=timezone)
    if parsed is None:
        return ScheduleOutcome(message=NOT_UNDERSTOOD, created=False)

    return await _create(uow, entry_id=entry_id, chat_id=chat_id, parsed=parsed, now=now)
