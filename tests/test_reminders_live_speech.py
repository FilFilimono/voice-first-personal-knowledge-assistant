"""Напоминания на живой речи — тексты взяты из базы дословно.

Отдельный файл от `test_reminders.py` намеренно. Там канонические формулировки,
которые автор кода считает правильными; здесь — то, что владелец сказал на самом
деле, со всеми «э-э», оборванными словами и мусором расшифровки.

Разница между этими двумя файлами и есть цена всех починок Фазы 1: каждый раз
код проходил свой набор и падал на речи. Поэтому правило файла: **строку сюда
можно добавить только скопировав её из базы.** Придуманная «похожая» строка
здесь хуже, чем её отсутствие, — она создаёт ощущение проверки.

Как добывалось: `select coalesce(t.text, e.raw_text) from entries e
left join transcripts t on t.source_entry_id = e.id`.
"""

from datetime import UTC, datetime

import pytest

from brain.core.scheduler import reminders

TIMEZONE = "Europe/Moscow"

# Вторник, 18 августа 2026, 13:08 по Москве — момент, когда владелец
# наговорил запись про офис.
NOW = datetime(2026, 8, 18, 10, 8, tzinfo=UTC)


def _parse(text: str, *, now: datetime = NOW) -> reminders.ParsedReminder | None:
    return reminders.parse(text, now=now, timezone=TIMEZONE)


# --- Записи 26 и 28: «уйти с офиса в 7:40 вечера» ---

# entry 26, голос. Ради неё всё и затевалось: «напомнил» вместо «напомни»,
# триггер в середине фразы, между ним и временем — «уси с офиса»
# (расшифровка съела «уйти»), и время сказано двенадцатичасовым.
OFFICE_VOICE = "Плюс, я хочу, чтобы ты меня напомнил, э, уси с офиса, э-э, в 7:40 вечера."

# entry 28, текст. Та же мысль, набранная руками через полминуты: владелец
# сам перевёл время в 24-часовое, потому что бот его не понял.
OFFICE_TEXT = "напомни мне уйти в 19:40 вечера домой"


def test_office_voice_is_understood() -> None:
    """Запись 26 целиком: и триггер в живой форме, и «вечера»."""
    parsed = _parse(OFFICE_VOICE)

    assert parsed is not None, "напоминание не разобрано — та самая дыра"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)
    assert parsed.fire_at.day == 18


def test_office_voice_keeps_what_to_do() -> None:
    """Смысл остаётся, «э-э» и время уходят."""
    parsed = _parse(OFFICE_VOICE)

    assert parsed is not None
    assert "офиса" in parsed.text
    assert "7:40" not in parsed.text
    assert "вечера" not in parsed.text


def test_office_text_does_not_shift_the_evening_twice() -> None:
    """«в 19:40 вечера» — уже 24-часовое время. Прибавлять двенадцать нельзя.

    Это доказано записью 28: она сработала верно только потому, что владелец
    сам перевёл время. Слово «вечера» при этом осело мусором в тексте
    напоминания — в базе лежит «мне уйти вечера домой».
    """
    parsed = _parse(OFFICE_TEXT)

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)
    assert "вечера" not in parsed.text


# --- Записи 16 и 18: «за 10 минут до 15:00» ---

# entry 18, текст. Прямой порядок слов.
LEAD_PLAIN = "напомни за 10 минут до 15:00 пойти домой"

# entry 16, текст. Триггер в середине, число после единицы («за минут 10»),
# а само время названо в начале фразы — «до этого» указывает назад.
LEAD_INVERTED = "а в 15:00 я ухожу домой, напомни мне за минут 10 до этого"


@pytest.mark.parametrize(
    "spoken",
    [
        pytest.param(LEAD_PLAIN, id="за 10 минут до"),
        pytest.param(LEAD_INVERTED, id="за минут 10 до этого"),
    ],
)
def test_lead_time_before_a_named_hour(spoken: str) -> None:
    """«за 10 минут до 15:00» — это 14:50, а не 15:00 и не «не понял»."""
    parsed = _parse(spoken)

    assert parsed is not None, "напоминание не разобрано"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (14, 50)


# --- Запись 17: время суток названо, но время уже 24-часовое ---

EVENING_WITH_EXACT_HOUR = "напомни мне вечером в 20:00 об 2 багах в сообщениях"


def test_daypart_next_to_a_24_hour_time_is_only_noise() -> None:
    """«вечером в 20:00» — двадцать ноль-ноль, и слово «вечером» в текст не идёт.

    В базе это напоминание лежит как «мне вечером об 2 багах в сообщениях».
    """
    parsed = _parse(EVENING_WITH_EXACT_HOUR)

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (20, 0)
    assert "вечером" not in parsed.text
    assert "багах" in parsed.text


# --- Записи, которые обязаны остаться обычными мыслями ---

# entry 8: время названо, «напомни» — нет.
THOUGHT_WITH_TIME = (
    "Также во вторник в 9:00 утра у меня экзамен по ГАИ. "
    "Я пока не чувствую себя уверенно, но посмотрим, чем это всё закончится."
)

# entry 25: «до 7:00-8:00 вечера» — это описание дня, а не просьба.
THOUGHT_WITH_EVENING = (
    "Я сегодня не сдал ГАИ, ценно, всё равно, я сегодня, мм, "
    "планирую быть в офисе до 7:00-8:00 вечера."
)


@pytest.mark.parametrize(
    "spoken",
    [
        pytest.param(THOUGHT_WITH_TIME, id="экзамен во вторник"),
        pytest.param(THOUGHT_WITH_EVENING, id="в офисе до вечера"),
    ],
)
def test_thought_with_a_time_is_still_a_thought(spoken: str) -> None:
    """Расширенный триггер не должен превращать архив в будильники."""
    assert _parse(spoken) is None
