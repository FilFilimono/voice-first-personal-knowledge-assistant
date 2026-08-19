"""Что должно работать:

1. «напомни завтра в 10» — завтра в десять утра по таймзоне владельца
2. день без времени — десять утра по умолчанию, а не полночь
3. «в 10», сказанное после десяти, — это про завтра, а не про прошлое
4. относительное время: «через 20 минут», «через час», «через 3 дня»
5. день недели, дата словами
6. без слова «напомни» напоминание не создаётся — иначе любая мысль
   со словом «завтра» становилась бы будильником
7. мусор во времени не превращается в наугад выбранный момент
"""

from datetime import UTC, datetime

import pytest

from brain.core.scheduler import reminders

TIMEZONE = "Europe/Moscow"
# Суббота, 15 августа 2026, 09:00 по Москве.
NOW = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)


def _parse(text: str, *, now: datetime = NOW) -> reminders.ParsedReminder | None:
    return reminders.parse(text, now=now, timezone=TIMEZONE)


def test_tomorrow_at_ten() -> None:
    parsed = _parse("напомни завтра в 10 позвонить в банк")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour, parsed.fire_at.minute) == (16, 10, 0)
    assert parsed.text == "позвонить в банк"


def test_day_without_time_defaults_to_morning() -> None:
    """Полночь была бы формально верной и практически бесполезной."""
    parsed = _parse("напомни завтра забрать посылку")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (16, 10)


def test_time_already_passed_today_means_tomorrow() -> None:
    evening = datetime(2026, 8, 15, 18, 0, tzinfo=UTC)  # 21:00 по Москве

    parsed = _parse("напомни в 10 выпить таблетки", now=evening)

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (16, 10)


def test_time_still_ahead_today_means_today() -> None:
    parsed = _parse("напомни в 18 позвонить маме")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (15, 18)


def test_minutes_are_kept() -> None:
    parsed = _parse("напомни завтра в 10:30 про созвон")

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (10, 30)


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("напомни через 20 минут проверить тесты", 20 / 60),
        ("напомни через час выйти", 1),
        ("напомни через 2 часа посмотреть логи", 2),
        ("напомни через 3 дня продлить домен", 72),
    ],
)
def test_relative_time(text: str, hours: float) -> None:
    parsed = _parse(text)

    assert parsed is not None
    delta = parsed.fire_at - NOW.astimezone(parsed.fire_at.tzinfo)
    assert delta.total_seconds() == pytest.approx(hours * 3600)


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        # Так говорят вслух — и так это приходит из расшифровки.
        ("напомни мне через две минуты открыть Discord", 2 / 60),
        ("напомни через одну минуту посмотреть календарь", 1 / 60),
        ("напомни через три часа про созвон", 3),
        ("напомни через пару минут глянуть почту", 2 / 60),
        ("напомни через пять дней продлить домен", 120),
        ("напомни через двадцать минут выйти", 20 / 60),
        ("напомни через полчаса поесть", 0.5),
        ("напомни через пол часа поесть", 0.5),
        ("напомни через полтора часа выехать", 1.5),
    ],
)
def test_numerals_spoken_as_words(text: str, hours: float) -> None:
    """Живая речь цифр не содержит.

    Пока этих случаев здесь не было, голосовые напоминания молча не работали:
    тесты подставляли «через 3 минуты», а владелец говорил «через две минуты».
    """
    parsed = _parse(text)

    assert parsed is not None, "напоминание не разобрано"
    delta = parsed.fire_at - NOW.astimezone(parsed.fire_at.tzinfo)
    assert delta.total_seconds() == pytest.approx(hours * 3600)


def test_word_numeral_does_not_eat_a_longer_one() -> None:
    """«двадцать» не должно разбираться как «два»."""
    parsed = _parse("напомни через двадцать минут выйти")

    assert parsed is not None
    assert (parsed.fire_at - NOW.astimezone(parsed.fire_at.tzinfo)).total_seconds() == 20 * 60


def test_reminder_text_survives_the_word_numeral() -> None:
    parsed = _parse("напомни мне через две минуты открыть Discord")

    assert parsed is not None
    assert "Discord" in parsed.text
    assert "через" not in parsed.text


def test_vague_amount_is_not_guessed() -> None:
    """«через несколько минут» — это не число. Угадывать нельзя."""
    assert _parse("напомни через несколько минут глянуть почту") is None


def test_weekday_points_to_the_next_one() -> None:
    """Суббота плюс «в понедельник» — ближайший понедельник."""
    parsed = _parse("напомни в понедельник в 9 отправить отчёт")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (17, 9)


def test_todays_weekday_means_next_week() -> None:
    """Сказанное в субботу «в субботу» — про следующую, а не про сегодня."""
    parsed = _parse("напомни в субботу подвести итоги")

    assert parsed is not None
    assert parsed.fire_at.day == 22


def test_date_in_words() -> None:
    parsed = _parse("напомни 15 сентября в 12 оплатить сервер")

    assert parsed is not None
    assert (parsed.fire_at.month, parsed.fire_at.day, parsed.fire_at.hour) == (9, 15, 12)


def test_past_date_moves_to_next_year() -> None:
    parsed = _parse("напомни 1 марта поздравить")

    assert parsed is not None
    assert (parsed.fire_at.year, parsed.fire_at.month) == (2027, 3)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("напомни в 7 вечера уйти с офиса", (19, 0)),
        ("напомни в 7:40 вечера уйти с офиса", (19, 40)),
        ("напомни в 8 утра выпить таблетки", (8, 0)),
        ("напомни в 9 часов вечера позвонить маме", (21, 0)),
        ("напомни в 11 вечера поставить телефон на зарядку", (23, 0)),
        ("напомни в час дня пообедать", (13, 0)),
        ("напомни в час ночи закрыть ноутбук", (1, 0)),
        ("напомни в 2 ночи проверить бэкап", (2, 0)),
        ("напомни в полдень пообедать", (12, 0)),
        ("напомни в полночь подвести итоги", (0, 0)),
    ],
)
def test_time_of_day(text: str, expected: tuple[int, int]) -> None:
    """Вслух говорят «в 7 вечера», а не «в 19:00».

    Пока этого не было, «напомни уйти с офиса в 7:40 вечера» либо не разбиралось
    вовсе, либо уехало бы на 7:40 утра — на двенадцать часов раньше нужного.
    """
    parsed = _parse(text)

    assert parsed is not None, "напоминание не разобрано"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == expected


def test_daypart_does_not_shift_a_24_hour_time() -> None:
    """«в 19:40 вечера» — уже вечер. Прибавлять двенадцать нельзя."""
    parsed = _parse("напомни в 19:40 вечера уйти домой")

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)


def test_daypart_leaves_no_trace_in_the_text() -> None:
    parsed = _parse("напомни в 7 вечера уйти с офиса")

    assert parsed is not None
    assert parsed.text == "уйти с офиса"


def test_bare_hour_that_already_passed_means_tomorrow() -> None:
    """«в 7» без уточнения, сказанное в девять утра, — это завтра.

    Правило неоднозначности: молча поставить напоминание в прошлое хуже,
    чем ошибиться на сутки вперёд, — в прошлом оно просто не сработает.
    """
    parsed = _parse("напомни в 7 позвонить в банк")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (16, 7)


def test_bare_hour_still_ahead_means_today() -> None:
    parsed = _parse("напомни в 19 позвонить в банк")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (15, 19)


def test_daypart_moves_a_passed_hour_to_tomorrow_too() -> None:
    """«в 8 утра», сказанное в девять утра, — тоже про завтра."""
    parsed = _parse("напомни в 8 утра выпить таблетки")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (16, 8)


@pytest.mark.parametrize(
    ("text", "expected_hour"),
    [
        ("напомни в среду вечером собрать вещи", 19),
        ("напомни в среду утром позвонить в банк", 9),
        ("напомни завтра днём заехать в сервис", 14),
    ],
)
def test_daypart_without_an_hour_sets_a_sensible_one(text: str, expected_hour: int) -> None:
    """«в среду вечером» — время названо, пусть и не по часам."""
    parsed = _parse(text)

    assert parsed is not None, "напоминание не разобрано"
    assert parsed.fire_at.hour == expected_hour


def test_end_of_day() -> None:
    parsed = _parse("напомни в конце дня отправить отчёт")

    assert parsed is not None
    assert (parsed.fire_at.day, parsed.fire_at.hour) == (15, 18)


@pytest.mark.parametrize(
    "text",
    [
        "напомни за 10 минут до 15:00 выйти",
        "напомни за десять минут до 15:00 пойти домой",
        "напомни за минут 10 до 15:00 пойти домой",
    ],
)
def test_lead_time_before_a_named_hour(text: str) -> None:
    """«за 10 минут до 15:00» — это 14:50."""
    parsed = _parse(text)

    assert parsed is not None, "напоминание не разобрано"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (14, 50)


def test_lead_time_before_a_daypart_hour() -> None:
    """«за полчаса до 7 вечера» — половина седьмого вечера."""
    parsed = _parse("напомни за полчаса до 7 вечера собраться")

    assert parsed is not None
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (18, 30)


@pytest.mark.parametrize(
    "text",
    [
        # Живая речь: форма глагола другая, а между триггером и временем
        # стоят посторонние слова.
        "напомни мне уйти с офиса в 19:40",
        "напомнить мне уйти с офиса в 19:40",
        "напомните мне уйти с офиса в 19:40",
        "я хочу чтобы ты мне напомнил уйти с офиса в 19:40",
        "слушай, напоминай мне уйти с офиса в 19:40",
    ],
)
def test_trigger_catches_the_root_in_any_form_and_any_place(text: str) -> None:
    """Триггер цепляется за корень «напомн», а не за одну форму в начале строки.

    Речь всегда кривая: расшифровка отдаёт «чтобы ты мне напомнил», а не
    «напомни». Якорь на начало строки и жёсткая форма глагола отсекали
    ровно то, как владелец говорит на самом деле.
    """
    parsed = _parse(text)

    assert parsed is not None, "напоминание не разобрано"
    assert (parsed.fire_at.hour, parsed.fire_at.minute) == (19, 40)


def test_words_between_the_trigger_and_the_time_survive() -> None:
    parsed = _parse("напомни мне уйти с офиса в 19:40")

    assert parsed is not None
    assert parsed.text == "уйти с офиса"


def test_thought_without_the_trigger_word_is_not_a_reminder() -> None:
    assert _parse("завтра в 10 встреча, надо подготовиться") is None
    assert _parse("через час буду дома") is None


def test_trigger_word_without_understandable_time_is_not_a_reminder() -> None:
    """Не понял — не угадываю: молча ошибиться со временем хуже, чем не завести."""
    assert _parse("напомни как-нибудь потом про это") is None


def test_impossible_time_is_not_a_reminder() -> None:
    assert _parse("напомни в 99 сделать дело") is None
