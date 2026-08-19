"""Напоминания на живой речи — тексты берутся из базы прямо в прогоне.

Отдельный файл от `test_reminders.py` намеренно. Там канонические формулировки,
которые автор кода считает правильными; здесь — то, что владелец сказал на самом
деле, со всеми «э-э», оборванными словами и мусором расшифровки.

Разница между этими двумя файлами и есть цена всех починок Фазы 1: каждый раз
код проходил свой набор и падал на речи.

**Правило файла было «строку можно добавить, только скопировав её из базы».
Теперь строк здесь нет вовсе.** Причина — приватность ([ADR-0002](../docs/adr/0002-privacy.md)):
архив наружу не уходит, и git это тоже наружу. Правило от этого только окрепло:
скопировать «похожую» строку физически негде, тест привязан к номеру записи,
а текст приезжает из базы.

**Цена названа вслух.** Нет базы и нет кэша — тесты скипаются, а пропущенный
тест зелёным не считается. На чистой машине и в CI этот файл ничего
не проверяет, и заметно это только по счётчику `skipped`.
"""

from datetime import UTC, datetime

import pytest

from brain.core.scheduler import reminders
from tests.archive import Entry, entry

TIMEZONE = "Europe/Moscow"

# Вторник, 18 августа 2026, 13:08 по Москве — момент, когда владелец
# наговорил запись про офис.
NOW = datetime(2026, 8, 18, 10, 8, tzinfo=UTC)

# Номера записей, а не их текст. Что в каждой — в комментарии: файл должен
# читаться и без базы под рукой.
#
# 26, голос. Ради неё всё и затевалось: глагол в прошедшем времени вместо
# повелительного, триггер в середине фразы, между ним и временем — обрывок
# слова, которое расшифровка не дослышала, и время в двенадцатичасовом виде.
OFFICE_VOICE = 26

# 28, текст. Та же мысль, набранная руками через полминуты: владелец сам
# перевёл время в 24-часовое, потому что бот его не понял.
OFFICE_TEXT = 28

# 18, текст. «за 10 минут до 15:00», прямой порядок слов.
LEAD_PLAIN = 18

# 16, текст. Триггер в середине, число после единицы («за минут 10»),
# а само время названо в начале фразы — «до этого» указывает назад.
LEAD_INVERTED = 16

# 17, текст. «вечером в 20:00»: время суток названо, но время уже 24-часовое.
EVENING_WITH_EXACT_HOUR = 17

# 8, голос. Время названо, «напомни» — нет: экзамен во вторник.
THOUGHT_WITH_TIME = 8

# 25, голос. «до 7:00-8:00 вечера» — описание дня, а не просьба.
THOUGHT_WITH_EVENING = 25


def _parse(
    archive: list[Entry], entry_id: int, *, now: datetime = NOW
) -> reminders.ParsedReminder | None:
    return reminders.parse(entry(archive, entry_id).text, now=now, timezone=TIMEZONE)


# --- Записи 26 и 28: «уйти с офиса в 7:40 вечера» ---


def test_office_voice_is_understood(archive: list[Entry]) -> None:
    """Запись 26 целиком: и триггер в живой форме, и «вечера»."""
    parsed = _parse(archive, OFFICE_VOICE)

    assert parsed is not None, "напоминание не разобрано — та самая дыра"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)
    assert parsed.fire_at.day == 18


def test_office_voice_keeps_what_to_do(archive: list[Entry]) -> None:
    """Смысл остаётся, «э-э» и время уходят."""
    parsed = _parse(archive, OFFICE_VOICE)

    assert parsed is not None
    assert "офиса" in parsed.text
    assert "7:40" not in parsed.text
    assert "вечера" not in parsed.text


def test_office_text_does_not_shift_the_evening_twice(archive: list[Entry]) -> None:
    """«в 19:40 вечера» — уже 24-часовое время. Прибавлять двенадцать нельзя.

    Это доказано записью 28: она сработала верно только потому, что владелец
    сам перевёл время. Слово «вечера» при этом осело мусором в тексте
    напоминания — в базе лежит «мне уйти вечера домой».
    """
    parsed = _parse(archive, OFFICE_TEXT)

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)
    assert "вечера" not in parsed.text


# --- Записи 16 и 18: «за 10 минут до 15:00» ---


@pytest.mark.parametrize(
    "entry_id",
    [
        pytest.param(LEAD_PLAIN, id="за 10 минут до"),
        pytest.param(LEAD_INVERTED, id="за минут 10 до этого"),
    ],
)
def test_lead_time_before_a_named_hour(archive: list[Entry], entry_id: int) -> None:
    """«за 10 минут до 15:00» — это 14:50, а не 15:00 и не «не понял»."""
    parsed = _parse(archive, entry_id)

    assert parsed is not None, "напоминание не разобрано"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (14, 50)


# --- Запись 17: время суток названо, но время уже 24-часовое ---


def test_daypart_next_to_a_24_hour_time_is_only_noise(archive: list[Entry]) -> None:
    """«вечером в 20:00» — двадцать ноль-ноль, и слово «вечером» в текст не идёт.

    В базе это напоминание лежит как «мне вечером об 2 багах в сообщениях».
    """
    parsed = _parse(archive, EVENING_WITH_EXACT_HOUR)

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (20, 0)
    assert "вечером" not in parsed.text
    assert "багах" in parsed.text


# --- Записи, которые обязаны остаться обычными мыслями ---


@pytest.mark.parametrize(
    "entry_id",
    [
        pytest.param(THOUGHT_WITH_TIME, id="экзамен во вторник"),
        pytest.param(THOUGHT_WITH_EVENING, id="в офисе до вечера"),
    ],
)
def test_thought_with_a_time_is_still_a_thought(archive: list[Entry], entry_id: int) -> None:
    """Расширенный триггер не должен превращать архив в будильники."""
    assert _parse(archive, entry_id) is None
