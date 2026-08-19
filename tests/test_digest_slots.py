"""Когда наступает момент касания.

Тесты быстрые и без базы: здесь только арифметика времени. Она вынесена
отдельно ровно затем, чтобы её можно было проверить без Postgres, — а проверить
её надо, потому что ошибка тут не падает, а сдвигает сообщения на час
и обнаруживается через месяц.

Главный тест здесь — про переход на зимнее время. Он и есть причина, по которой
в проекте нет `cron 0 9 * * *`.
"""

from datetime import UTC, datetime

from brain.core.digest import slots
from brain.storage.models.touch import TouchSlot

ROME = "Europe/Rome"


def utc(text: str) -> datetime:
    """«2026-08-20 07:00» в UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_morning_slot_opens_at_nine_local_not_utc() -> None:
    # Летом Рим — UTC+2, значит 09:00 по Риму это 07:00 UTC.
    assert TouchSlot.MORNING not in slots.due(utc("2026-08-20 06:59"), ROME)
    assert TouchSlot.MORNING in slots.due(utc("2026-08-20 07:00"), ROME)


def test_winter_time_shift_does_not_move_the_morning_summary() -> None:
    """Ради этого теста в проекте нет фиксированного cron.

    Европа переводит часы в последнее воскресенье октября: 25 октября 2026.
    До перевода 09:00 по Риму — это 07:00 UTC, после — 08:00 UTC. Расписание
    `0 9 * * *` в UTC приходило бы либо на час раньше, либо на час позже,
    и заметить это можно было бы только по ощущению.
    """
    summer = utc("2026-10-20 07:00")
    winter = utc("2026-10-27 08:00")

    assert TouchSlot.MORNING in slots.due(summer, ROME)
    assert TouchSlot.MORNING in slots.due(winter, ROME)

    # А в те же моменты UTC по другую сторону перевода — ещё рано.
    assert TouchSlot.MORNING not in slots.due(utc("2026-10-27 06:59"), ROME)


def test_morning_summary_does_not_come_after_noon() -> None:
    """Сводка про сегодняшний день, а в 14:00 она уже не про него.

    Воркер, поднявшийся днём после ночного простоя, не должен присылать
    утреннюю сводку задним числом: это ровно то сообщение про прошедшее,
    которое запрещено принципом 2 из `vision.md` §7.0.
    """
    assert TouchSlot.MORNING in slots.due(utc("2026-08-20 09:30"), ROME)  # 11:30 в Риме
    assert TouchSlot.MORNING not in slots.due(utc("2026-08-20 10:30"), ROME)  # 12:30


def test_weekly_review_only_on_sunday() -> None:
    # 23 августа 2026 — воскресенье, 24-е — понедельник.
    assert TouchSlot.WEEKLY in slots.due(utc("2026-08-23 18:30"), ROME)  # 20:30 в Риме
    assert TouchSlot.WEEKLY not in slots.due(utc("2026-08-24 18:30"), ROME)


def test_slots_come_in_order_of_their_hour() -> None:
    """Порядок в списке — порядок отправки, и он должен быть хронологическим.

    В воскресенье вечером созревают два слота сразу: если воркер поднялся
    в 21:30, он отправит их подряд, и читаться они должны как вечер, а не как
    случайная стопка. Ревью в 20:00 идёт раньше разбора в 21:00.
    """
    due = slots.due(utc("2026-08-23 19:30"), ROME)  # 21:30 в Риме, воскресенье
    assert due == [TouchSlot.WEEKLY, TouchSlot.EVENING]


def test_day_slot_closes_before_the_evening() -> None:
    """Всплывание старого — дневное касание, и в десять вечера оно уже не дневное.

    Окно нужно не ради красоты: воркер, поднявшийся ночью, иначе высыпал бы
    разом всё, что не отправил за день, — четыре сообщения подряд в полночь.
    """
    assert TouchSlot.DAY in slots.due(utc("2026-08-20 11:00"), ROME)  # 13:00 в Риме
    assert TouchSlot.DAY not in slots.due(utc("2026-08-20 18:00"), ROME)  # 20:00


def test_local_date_is_the_owners_day_not_the_servers() -> None:
    """Сутки бюджета кончаются в полночь у владельца, а не в полночь UTC.

    В 23:30 по Риму в UTC уже 21:30 того же дня, а в 00:30 по Риму —
    22:30 **предыдущего**. Бюджет, посчитанный по UTC, разрешил бы четвёртое
    касание за один вечер.
    """
    assert slots.local_date(utc("2026-08-20 22:30"), ROME).isoformat() == "2026-08-21"
    assert slots.local_date(utc("2026-08-20 21:30"), ROME).isoformat() == "2026-08-20"
