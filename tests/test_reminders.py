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


def test_thought_without_the_trigger_word_is_not_a_reminder() -> None:
    assert _parse("завтра в 10 встреча, надо подготовиться") is None
    assert _parse("через час буду дома") is None


def test_trigger_word_without_understandable_time_is_not_a_reminder() -> None:
    """Не понял — не угадываю: молча ошибиться со временем хуже, чем не завести."""
    assert _parse("напомни как-нибудь потом про это") is None


def test_impossible_time_is_not_a_reminder() -> None:
    assert _parse("напомни в 99 сделать дело") is None
