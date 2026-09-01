"""Разбор повтора: «каждый день», «по будням», «два раза в неделю».

Соседний модуль `reminders` разбирает **момент** — «завтра в 10», «через две
минуты». Здесь разбирается **ритм**, и это другая величина: у момента есть
дата, у ритма её нет вовсе. Смешивать их нельзя, и `vision.md` §5.11 говорит
об этом прямо: «на этой неделе» и «до пятницы» ритмом **не являются** — это
задача с окном, а не привычка.

**Разбирает код, а не модель** — принцип 12 и заодно квота: привычка
напоминает каждый день, и вызов модели на каждое напоминание стоил бы больше
всей остальной системы. Та же причина, по которой кодом разбираются
и напоминания.

**Морфологии нет.** Формы слов перечислены руками — как `IMPERATIVE_VERBS`
в `reminders`. Незнакомая форма означает «ритма не нашли», а не неверный ритм:
система тогда **спрашивает**, и это ровно то поведение, которое требует §5.11
(«ритма в записи нет — бот спрашивает, а не угадывает»). Ошибка идёт в сторону
вопроса, а не в сторону выдуманного ежедневного сообщения.

Словарь дней недели живёт здесь, а не в `reminders`, чтобы не заводить цикл
импорта: `reminders` знает про ритм (он задаёт вопрос про привычку), а ритм
про напоминания не знает ничего.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from brain.storage.models.habit import Rhythm

# Дни недели в тех формах, которыми пользуются вслух. Именительный и косвенные
# для одиночного дня («в среду»), дательное множественное для ритма
# («по средам»). Значение — номер дня, как у `datetime.weekday()`.
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

# «по понедельникам», «каждый вторник» — только эти формы означают повтор.
# «в пятницу» сюда не входит намеренно: это один раз, а не ритм.
WEEKDAYS_REPEATED: dict[str, int] = {
    "понедельникам": 0,
    "понедельник": 0,
    "вторникам": 1,
    "вторник": 1,
    "средам": 2,
    "среду": 2,
    "среда": 2,
    "четвергам": 3,
    "четверг": 3,
    "пятницам": 4,
    "пятницу": 4,
    "пятница": 4,
    "субботам": 5,
    "субботу": 5,
    "суббота": 5,
    "воскресеньям": 6,
    "воскресенье": 6,
}

WORKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
DAYS_IN_WEEK = 7

# Числительные словами: «два раза в неделю» вслух говорят именно так.
# Список свой, а не общий с `reminders`: там числительные считают минуты
# и часы, здесь — разы в неделю, и общими они выглядят только на вид.
TIMES: dict[str, int] = {
    "раз": 1,
    "один": 1,
    "два": 2,
    "две": 2,
    "дважды": 2,
    "три": 3,
    "трижды": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
}

_TIMES_ALTERNATION = "|".join(sorted(TIMES, key=len, reverse=True))
_REPEATED_ALTERNATION = "|".join(sorted(WEEKDAYS_REPEATED, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class Recurrence:
    """Ритм, вытащенный из слов записи."""

    rhythm: Rhythm
    # Дни недели, если ритм их называет. У «каждый день» и «через день» пусто.
    days: tuple[int, ...] = ()
    # Сколько раз в неделю, если сказано числом. Нужно для показа: «два раза
    # в неделю» и «по понедельникам и четвергам» — разные фразы, хотя дни могут
    # совпасть, и владельцу надо вернуть его собственные слова.
    times_per_week: int | None = None


# Ритм, а не срок. Порядок правил важен: «каждый день» проверяется раньше
# «по будням», потому что фраза может содержать оба слова.
DAILY = re.compile(r"\bкажд(?:ый|ое|ую)\s+(?:день|утро|вечер|ночь)\b|\bежедневн\w*", re.IGNORECASE)
DAILY_DAYPART = re.compile(r"\bпо\s+(?:утрам|вечерам|ночам)\b", re.IGNORECASE)
EVERY_OTHER_DAY = re.compile(r"\bчерез\s+день\b", re.IGNORECASE)
WEEKDAYS_PATTERN = re.compile(
    r"\bпо\s+будням\b|\bв\s+будни\b|\bпо\s+рабочим\s+дням\b", re.IGNORECASE
)
WEEKENDS_PATTERN = re.compile(r"\bпо\s+выходным\b|\bв\s+выходные\b", re.IGNORECASE)
NAMED_DAYS = re.compile(
    rf"\b(?:по|кажд(?:ый|ую|ое))\s+(?P<day>{_REPEATED_ALTERNATION})\b", re.IGNORECASE
)
PER_WEEK = re.compile(
    rf"\b(?P<count>\d+|{_TIMES_ALTERNATION})\s*раз\w*\s+в\s+недел\w+\b", re.IGNORECASE
)
WEEKLY = re.compile(r"\bкажд(?:ую|ые)\s+недел\w+\b|\bеженедельн\w*", re.IGNORECASE)

# **Окно — не ритм, и отдельного правила против него здесь нет намеренно.**
# «На этой неделе дочитать книгу», «до пятницы разобраться» — задача со сроком
# (`vision.md` §5.11), и ни один из образцов выше в них не попадает: ритм
# по-русски называется словами «каждый», «по …ам», «N раз в неделю», а срок —
# словами «на», «до», «за». Отдельный запрет отсекал бы заодно и настоящий
# ритм со сроком («по будням до конца месяца»), а ошибка в эту сторону дороже:
# пропущенный ритм система спрашивает, отсечённый теряет молча.
# Держится это тестом, а не комментарием.

# Время суток, названное вместе с ритмом: «каждое утро», «по вечерам».
# Час берётся тот же, что у напоминаний, — иначе «вечером» означало бы разное
# в двух местах системы.
DAYPART_HOUR: dict[str, int] = {"утр": 9, "день": 14, "дня": 14, "вечер": 19, "ноч": 23}
DAYPART = re.compile(r"\b(утр\w+|вечер\w+|ноч\w+)\b", re.IGNORECASE)


def parse(text: str) -> Recurrence | None:
    """Ритм из слов записи. `None` — ритма нет, и это не ошибка.

    `None` означает «спросить», а не «не привычка»: решение, привычка это или
    нет, принимает владелец кнопкой, а код только предлагает (`vision.md` §5.11).
    """
    if DAILY.search(text) or DAILY_DAYPART.search(text):
        return Recurrence(Rhythm.DAILY)

    if EVERY_OTHER_DAY.search(text):
        return Recurrence(Rhythm.EVERY_OTHER_DAY)

    if WEEKDAYS_PATTERN.search(text):
        return Recurrence(Rhythm.WEEKDAYS, days=WORKDAYS)

    if WEEKENDS_PATTERN.search(text):
        return Recurrence(Rhythm.WEEKENDS, days=WEEKEND)

    named = tuple(
        sorted(
            {WEEKDAYS_REPEATED[match.group("day").lower()] for match in NAMED_DAYS.finditer(text)}
        )
    )
    if named:
        return Recurrence(Rhythm.WEEKLY_DAYS, days=named)

    if match := PER_WEEK.search(text):
        return Recurrence(Rhythm.TIMES_PER_WEEK, times_per_week=_count_of(match.group("count")))

    if WEEKLY.search(text):
        return Recurrence(Rhythm.TIMES_PER_WEEK, times_per_week=1)

    return None


def _count_of(spoken: str) -> int:
    return int(spoken) if spoken.isdigit() else TIMES[spoken.lower()]


# Граница предложения в живой речи. Знака в конце может не быть вовсе —
# расшифровка сплошь и рядом идёт без точек, — поэтому хвост без знака тоже
# считается предложением.
SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def clause(text: str) -> str:
    """Предложение, в котором назван ритм. Ритма нет — текст целиком.

    Нужно затем, что текст привычки показывается **каждый день**, и взятая
    целиком смысловая единица притащила бы туда соседнюю фразу. На живой
    записи 53 это «Книгу у меня опять не получилось вчера почитать» — то есть
    ретроспективный упрёк (`vision.md` §7.0, принцип 2), сказанный ежедневно
    и словами самого владельца. Фраза с ритмом при этом смотрит вперёд
    по построению: «начну с этого дня каждый день её читать».

    Режет **код, а не модель**: это происходит на нажатии кнопки, то есть
    в тот момент, когда владелец ждёт ответа, и вызов модели тут стоил бы
    секунд ожидания и запроса из двадцати суточных.
    """
    for match in SENTENCE.finditer(text):
        sentence = match.group().strip()
        if sentence and parse(sentence) is not None:
            return sentence
    return text.strip()


def hour_of(text: str, default: int) -> int:
    """Час напоминания, если время суток названо словом. Иначе — умолчание.

    Умолчание здесь законно, в отличие от ритма: час — это «когда именно
    в течение дня», и ошибка в нём сдвигает сообщение, а не заводит его.
    Ритм же угадывать нельзя вовсе — там ошибка означает ежедневное сообщение,
    о котором не просили.
    """
    match = DAYPART.search(text)
    if match is None:
        return default
    word = match.group(1).lower()
    for prefix, hour in DAYPART_HOUR.items():
        if word.startswith(prefix):
            return hour
    return default  # pragma: no cover — регулярка не пропустит чужое слово


def spread(times: int, *, anchor_weekday: int) -> tuple[int, ...]:
    """Дни недели для «N раз в неделю», размазанные от дня подтверждения.

    Ритм владелец назвал сам, а дни — нет, и брать их с потолка нельзя;
    но и спрашивать «в какие именно дни» после «два раза в неделю» значит
    требовать точности, которой в замысле не было. Поэтому дни считаются:
    первый — тот, в который привычка заведена, остальные равномерно дальше
    по неделе. «Два раза» от понедельника — понедельник и четверг.

    Это единственное место, где система решает за владельца, и решает она
    **меньшее**: не «как часто» (сказано), а «в какие дни» (не сказано).
    """
    if times >= DAYS_IN_WEEK:
        return tuple(range(DAYS_IN_WEEK))
    return tuple(
        sorted(
            {
                (anchor_weekday + round(step * DAYS_IN_WEEK / times)) % DAYS_IN_WEEK
                for step in range(times)
            }
        )
    )


def chosen(rhythm: Rhythm) -> Recurrence:
    """Ритм, названный кнопкой, а не словами записи.

    Дни здесь проставляются **сразу**, и это не удобство. `occurs_on` считает,
    что ритм по дням недели без единого дня не наступает никогда (и правильно
    считает: ежедневное сообщение по ошибке хуже пропущенного). Значит
    `Recurrence(Rhythm.WEEKDAYS)` с пустыми днями — это привычка, которая
    завелась, показалась владельцу и молчит навсегда. Такое ловится не тестом
    на функцию, а неделей тишины.

    `WEEKLY_DAYS` сюда не попадает: названные дни («по понедельникам
    и четвергам») приходят только из слов записи — кнопкой их пришлось бы
    собирать из семи нажатий.
    """
    if rhythm is Rhythm.WEEKDAYS:
        return Recurrence(rhythm, days=WORKDAYS)
    if rhythm is Rhythm.WEEKENDS:
        return Recurrence(rhythm, days=WEEKEND)
    if rhythm is Rhythm.TIMES_PER_WEEK:
        # Кнопка называется «Раз в неделю»: сколько именно раз, кнопкой
        # не спрашивают — за большим владелец скажет словами.
        return Recurrence(rhythm, times_per_week=1)
    return Recurrence(rhythm)


def days_of(recurrence: Recurrence, *, anchor_weekday: int) -> tuple[int, ...]:
    """Дни недели, в которые привычка наступает. Пусто — ритм не по дням."""
    if recurrence.rhythm is Rhythm.TIMES_PER_WEEK and recurrence.times_per_week:
        return spread(recurrence.times_per_week, anchor_weekday=anchor_weekday)
    return recurrence.days


# Насколько далеко вперёд ищется следующий день. Две недели с запасом
# перекрывают любой ритм, который система умеет разбирать, — а бесконечный
# цикл на неверных данных (пустой список дней у ритма «по дням») превратился бы
# в зависший воркер, то есть в самое дорогое из возможных последствий.
HORIZON_DAYS = 15


def occurs_on(day: date, *, rhythm: Rhythm, days: Sequence[int], anchor: date) -> bool:
    """Наступает ли привычка в этот день.

    `anchor` нужен только «через день»: чётность считается от дня, в который
    привычка заведена, — иначе «через день» означало бы разное в зависимости
    от того, с какого дня смотреть.
    """
    if rhythm is Rhythm.DAILY:
        return True
    if rhythm is Rhythm.EVERY_OTHER_DAY:
        return (day - anchor).days % 2 == 0
    if not days:
        # Ритм по дням недели без единого дня — это данные, которых не бывает.
        # Считаем, что не наступает никогда: ежедневное сообщение по ошибке
        # хуже пропущенного (`vision.md` §7.0, принцип 1).
        return False
    return day.weekday() in days


def next_occurrence(
    *,
    rhythm: Rhythm,
    days: Sequence[int],
    anchor: date,
    after: datetime,
    hour: int,
    minute: int = 0,
) -> datetime | None:
    """Ближайший момент напоминания **строго после** `after`.

    `after` и ответ — в таймзоне владельца: привычка «каждый день в 9» это
    девять утра по его календарю, а не по серверному UTC. Перевод часов
    от этого не ломается, потому что момент собирается из даты и часа заново,
    а не прибавлением суток к прошлому.

    `None` — ритм не наступает вовсе в ближайшие две недели. Такого при живых
    данных не бывает, и молчаливая единица здесь была бы хуже: пустой ответ
    видно, а сдвинутое на месяц напоминание — нет.
    """
    for offset in range(HORIZON_DAYS):
        day = after.date() + timedelta(days=offset)
        if not occurs_on(day, rhythm=rhythm, days=days, anchor=anchor):
            continue
        moment = datetime.combine(day, time(hour, minute), tzinfo=after.tzinfo)
        if moment > after:
            return moment
    return None


NAMES: dict[Rhythm, str] = {
    Rhythm.DAILY: "каждый день",
    Rhythm.EVERY_OTHER_DAY: "через день",
    Rhythm.WEEKDAYS: "по будням",
    Rhythm.WEEKENDS: "по выходным",
    Rhythm.WEEKLY_DAYS: "по дням недели",
    Rhythm.TIMES_PER_WEEK: "раз в неделю",
}

DAY_NAMES = (
    "понедельникам",
    "вторникам",
    "средам",
    "четвергам",
    "пятницам",
    "субботам",
    "воскресеньям",
)

TIMES_NAMES = {1: "раз в неделю", 2: "два раза в неделю", 3: "три раза в неделю"}


def describe(recurrence: Recurrence) -> str:
    """Ритм словами — тем же, какими его сказал владелец.

    Возвращать надо его формулировку, а не нашу: «два раза в неделю»,
    превращённое в «по понедельникам и четвергам», читается как чужое решение,
    даже когда дни выбраны верно.
    """
    if recurrence.rhythm is Rhythm.WEEKLY_DAYS:
        return "по " + " и ".join(DAY_NAMES[day] for day in recurrence.days)
    if recurrence.rhythm is Rhythm.TIMES_PER_WEEK and recurrence.times_per_week:
        return TIMES_NAMES.get(
            recurrence.times_per_week, f"{recurrence.times_per_week} раза в неделю"
        )
    return NAMES[recurrence.rhythm]
