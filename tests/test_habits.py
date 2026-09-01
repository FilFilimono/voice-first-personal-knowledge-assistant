"""Привычки: то, что делается регулярно и напоминает о себе само.

`vision.md` §5.11. Что здесь проверяется и почему именно это:

1. разметка знает про десятый тип — промпт собирается, а не падает
2. ритм разбирается из слов записи, а окно («на этой неделе») ритмом не считается
3. текст привычки — фраза с ритмом, а не вся единица: он показывается **каждый
   день**, и соседнее «опять не получилось вчера» приезжало бы вместе с ним
4. напоминание несёт контекст и вопрос про ход, а не «сделал или нет»
5. спросив сегодня, тик не спрашивает второй раз — иначе окно в шесть часов
   даёт семьдесят два одинаковых сообщения за утро

Быстрые тесты, кроме одного: путь целиком живёт в `test_habits_path.py`
и требует базы. Разделены намеренно — правило проекта «тест на функцию
не заменяет теста на путь» значит **и то, и другое**, а не одно вместо другого.

Тексты живой речи берутся из базы по номеру записи (`tests/archive.py`).
"""

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.digest import habits, render
from brain.core.enrich import prompt
from brain.core.present import guard
from brain.core.retrieval.links import Link
from brain.core.scheduler import recurrence
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.habit import Habit, HabitStatus, Rhythm
from tests import archive as archive_module

TIMEZONE = "Europe/Rome"

# Живая запись, в которой владелец назвал привычку словами: «И начну с этого
# дня каждый день её читать. Минимум 10 страниц». Выбрана не за удобство —
# в ней же рядом стоит «Книгу у меня опять не получилось вчера почитать»,
# то есть ровно та фраза, которую нельзя показывать ежедневно.
HABIT_ENTRY = 53
REPROACH_IN_THE_SAME_UNIT = "опять не получилось"


def _habit(
    *,
    rhythm: Rhythm = Rhythm.DAILY,
    days: list[int] | None = None,
    hour: int = 9,
    created_at: datetime | None = None,
    last_reminded_at: datetime | None = None,
    text: str = "начну с этого дня каждый день её читать",
) -> Habit:
    """Привычка в памяти, без базы: `is_due` и рендер её не пишут."""
    item = Habit()
    item.id = 1
    item.source_entry_id = HABIT_ENTRY
    item.source_unit_id = None
    item.created_at = created_at or datetime(2026, 8, 20, 7, 0, tzinfo=UTC)
    item.text = text
    item.rhythm = rhythm
    item.days = days
    item.times_per_week = None
    item.hour = hour
    item.minute = 0
    item.status = HabitStatus.ACTIVE
    item.last_reminded_at = last_reminded_at
    return item


# --- Разметка знает про привычку ---


def test_the_prompt_defines_the_habit_type() -> None:
    """Без определения промпт не собирается вовсе — `build_system` падал `KeyError`.

    Это не про качество формулировки, а про живой путь: список типов строится
    циклом по enum'у, и значение без определения роняет **всю разметку**,
    а не только привычки.
    """
    system = prompt.build_system([])

    assert AnnotationType.HABIT in prompt.DEFINITIONS
    assert "habit —" in system
    assert "из десяти" in system


def test_the_prompt_separates_a_habit_from_a_task_with_a_window() -> None:
    """«На этой неделе» — задача со сроком, а не привычка (`vision.md` §5.11).

    Граница названа модели прямо: повтор, а не срок. Без неё «на этой неделе
    дочитать» уехало бы в ежедневное сообщение.
    """
    definition = prompt.DEFINITIONS[AnnotationType.HABIT]

    assert "на этой неделе" in definition
    assert "task" in definition


# --- Ритм из слов записи ---


def test_the_rhythm_comes_from_the_words_of_a_live_entry(
    archive: list[archive_module.Entry],
) -> None:
    """«Каждый день» в живой записи читается как ежедневный ритм."""
    body = archive_module.entry(archive, HABIT_ENTRY).text

    found = recurrence.parse(body)

    assert found is not None
    assert found.rhythm is Rhythm.DAILY


@pytest.mark.parametrize(
    "said",
    [
        "надо на этой неделе дочитать книгу",
        "до пятницы разобраться с отчётом",
        "за выходные закончить главу",
    ],
)
def test_a_window_is_not_a_rhythm(said: str) -> None:
    """Срок ритмом не является — иначе задача превратилась бы в ежедневное сообщение."""
    assert recurrence.parse(said) is None


def test_no_rhythm_means_ask_rather_than_guess() -> None:
    """Умолчания у ритма нет: «хочу читать книгу» — это вопрос, а не «каждый день»."""
    assert recurrence.parse("хочу читать книгу") is None


# --- Текст привычки ---


def test_the_habit_text_is_the_sentence_that_names_the_rhythm(
    archive: list[archive_module.Entry],
) -> None:
    """Из живой единицы берётся фраза с ритмом, а не вся единица.

    Соседняя фраза той же записи — «Книгу у меня опять не получилось вчера
    почитать». Показанная вместе с привычкой, она приходила бы **каждый день**:
    ретроспективный упрёк словами самого владельца (`vision.md` §7.0, принцип 2).
    """
    body = archive_module.entry(archive, HABIT_ENTRY).text
    assert REPROACH_IN_THE_SAME_UNIT in body, "запись выбрана за эту фразу — её не стало"

    clause = recurrence.clause(body)

    assert "каждый день" in clause
    assert REPROACH_IN_THE_SAME_UNIT not in clause
    assert guard.reproach(clause) is None


# --- Напоминание ---


def test_the_reminder_carries_context_and_asks_about_progress() -> None:
    """Напоминание — повод рассказать, а не будильник (`vision.md` §5.11).

    Проверяется и то, и другое сразу: голое название это будильник, а вопрос
    «сделал?» — счётчик, запрещённый §7.0 п.5. Контекст — слова самого
    владельца из прежней записи, найденные векторной близостью.
    """
    said = Link(
        entry_id=39,
        text="обязательно книга каждый день минимум 10 страниц, сила в постоянстве",
        created_at=datetime(2026, 8, 21, 18, 0, tzinfo=UTC),
        timezone=TIMEZONE,
        similarity=0.88,
    )

    text = render.habit(_habit(), [said], timezone=TIMEZONE)

    assert "каждый день её читать" in text
    assert "сила в постоянстве" in text
    assert "21.08" in text
    assert text.endswith("Как продвижение? Что получается, что мешает?")
    assert "сделал" not in text.lower()


def test_the_reminder_says_nothing_about_yesterday() -> None:
    """Ни счётчика пропусков, ни серии, ни «третий день» (`vision.md` §5.11).

    Привычка заведена неделю назад и не спрашивалась четыре дня — в тексте
    об этом нет ни слова, и взяться ему неоткуда: данных нет по построению.
    """
    text = render.habit(
        _habit(
            created_at=datetime(2026, 8, 14, 7, 0, tzinfo=UTC),
            last_reminded_at=datetime(2026, 8, 17, 7, 0, tzinfo=UTC),
        ),
        timezone=TIMEZONE,
    )

    assert guard.reproach(text) is None
    for counted in ("день подряд", "пропустил", "дней", "раз из"):
        assert counted not in text


def test_the_reminder_survives_without_context() -> None:
    """Связей нет — обычный исход, а не сбой: остаётся ритм и вопрос."""
    text = render.habit(_habit(), timezone=TIMEZONE)

    assert "Каждый день" in text
    assert render.HABIT_QUESTION in text


# --- Когда спрашивать ---

# 09:30 по Риму — привычка назначена на девять, окно ещё открыто.
MORNING = datetime(2026, 8, 24, 7, 30, tzinfo=UTC)


def test_a_daily_habit_is_due_in_its_hour() -> None:
    assert recurrence.occurs_on(MORNING.date(), rhythm=Rhythm.DAILY, days=(), anchor=MORNING.date())


def test_asked_today_is_not_asked_again() -> None:
    """Главное утверждение задачи: тик приходит каждые пять минут.

    Без этой проверки шестичасовое окно даёт семьдесят два одинаковых
    сообщения за одно утро — и это не догадка, а арифметика `LATE`
    и `TOUCH_SCHEDULE`.
    """
    asked = MORNING - timedelta(minutes=5)
    item = _habit(last_reminded_at=asked)

    assert habits.is_due(item, now=MORNING, timezone=TIMEZONE) is False


def test_asked_yesterday_is_asked_again_today() -> None:
    """Вчерашний вопрос сегодняшнего не отменяет — привычка наступает снова."""
    item = _habit(last_reminded_at=MORNING - timedelta(days=1))

    assert habits.is_due(item, now=MORNING, timezone=TIMEZONE) is True


def test_a_habit_is_not_asked_before_its_hour() -> None:
    item = _habit(hour=19)

    assert habits.is_due(item, now=MORNING, timezone=TIMEZONE) is False


def test_a_missed_habit_is_never_delivered_late() -> None:
    """Проспали окно — молчим. Пропущенное напоминание не досылается никогда:
    привычка наступает снова, в свой срок, и вчерашнее не упоминается."""
    midnight = datetime(2026, 8, 24, 22, 30, tzinfo=UTC)
    item = _habit(hour=9)

    assert habits.is_due(item, now=midnight, timezone=TIMEZONE) is False
