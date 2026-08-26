"""Повтор касаний по прошедшим дням живого архива.

**Какой режим воспроизводит.** Тот же, что воркер: решение по каждому слоту
принимается в момент открытия его окна, в таймзоне владельца, по тем же
функциям сбора и той же сборке текста (`core/digest`). Одна отправка на слот
в сутки — как держит уникальность в журнале. Модель не зовётся ни разу,
потому что её не зовёт и прод (ADR-0013, ось 4).

**Чего повтор не воспроизводит, и это важнее того, что воспроизводит:**

- **нажатий.** Дела в повторе никто не закрывает, петли никто не откладывает.
  Значит, это верхняя граница содержания сводки, а не то, что владелец увидел
  бы на самом деле: в жизни список дел таял бы день ото дня;
- **бюджета и паузы.** Обе зависят от журнала касаний, которого за прошедшие
  дни не существует. Повтор считает, что бюджет цел и тишины нет;
- **накопления.** Всё считается по **сегодняшнему** состоянию базы, а не по
  состоянию на тот день: записи, размеченные позже, в повторе выглядят так,
  будто были размечены сразу.

**Ничего не пишет.** Ни строки в журнал касаний, ни сообщения в Telegram.
Это чтение живой базы и печать того, что ушло бы.

Прогон: `just eval-digest` или `uv run python -m evals.digest.replay`.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from brain.config import get_settings
from brain.core.digest import evening, morning, render, resurface, slots, weekly
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.touch import SilenceReason, TouchSlot

# Порядок разбора слотов — хронологический, как в `slots.due`.
ORDER = (TouchSlot.MORNING, TouchSlot.DAY, TouchSlot.WEEKLY, TouchSlot.EVENING)


@dataclass(frozen=True, slots=True)
class Outcome:
    """Что случилось бы со слотом в этот день."""

    slot: TouchSlot
    text: str | None
    silence: SilenceReason | None


async def _archive_days(timezone: str) -> list[datetime]:
    """Дни, за которые есть записи. Считается по архиву, а не по календарю."""
    async with session_scope() as session:
        bounds = (
            await session.execute(select(func.min(Entry.created_at), func.max(Entry.created_at)))
        ).one()

    if bounds[0] is None:
        return []

    zone = ZoneInfo(timezone)
    first = bounds[0].astimezone(zone).date()
    last = bounds[1].astimezone(zone).date()

    days = []
    current = first
    while current <= last:
        days.append(datetime.combine(current, datetime.min.time(), tzinfo=zone))
        current += timedelta(days=1)
    return days


async def _slot(slot: TouchSlot, moment: datetime, timezone: str, shown: set[int]) -> Outcome:
    """Что ушло бы по этому слоту в этот момент.

    `shown` — что вечер уже показывал за прошлые дни повтора. Список ведётся
    в памяти, потому что журнала касаний за прошедшие дни не существует;
    в проде то же множество приходит из журнала.
    """
    if slot is TouchSlot.MORNING:
        content = await morning.collect(moment)
        if content.empty:
            return Outcome(slot, None, SilenceReason.NOTHING_TO_SAY)
        return Outcome(slot, render.morning(content), None)

    if slot is TouchSlot.DAY:
        candidates = await resurface.eligible(moment)
        if not candidates:
            return Outcome(slot, None, SilenceReason.NOTHING_TO_SAY)
        # Берётся первый, а не случайный: повтор обязан быть воспроизводимым,
        # иначе два прогона по одному архиву дадут разные числа и сравнивать
        # их будет нечем. В проде здесь случайный выбор (`resurface.pick`).
        return Outcome(slot, render.resurfaced(candidates[0], timezone=timezone), None)

    if slot is TouchSlot.EVENING:
        disputed = await evening.disputed(limit=3, exclude=shown)
        if not disputed:
            return Outcome(slot, None, SilenceReason.NOTHING_TO_SAY)
        shown.update(item.entry_id for item in disputed)
        return Outcome(slot, render.evening(disputed), None)

    review = await weekly.collect(moment)
    if review is None:
        return Outcome(slot, None, SilenceReason.NOTHING_TO_SAY)
    return Outcome(slot, render.weekly(review), None)


REASONS = {
    SilenceReason.NOTHING_TO_SAY: "нечего сказать",
    SilenceReason.QUIET: "просили молчать",
    SilenceReason.OVER_BUDGET: "бюджет исчерпан",
}


async def run(timezone: str) -> None:
    days = await _archive_days(timezone)
    if not days:
        print("Архив пуст — повторять нечего.")
        return

    print(f"Повтор касаний, таймзона {timezone}. Дней в архиве: {len(days)}.")
    print("Режим: как у воркера. Нажатий, бюджета и паузы повтор не воспроизводит.\n")

    sent = 0
    silent = 0
    # Что вечер уже показывал. В проде это множество приходит из журнала
    # касаний; здесь его приходится вести в памяти, потому что журнала
    # за прошедшие дни не существует.
    shown: set[int] = set()
    for day in days:
        print(f"=== {day:%d.%m.%Y, %A} ===")
        for slot in ORDER:
            opens = slots.WINDOWS[slot][0]
            moment = day.replace(hour=opens)
            if slot not in slots.due(moment, timezone):
                continue

            outcome = await _slot(slot, moment, timezone, shown)
            if outcome.text is None:
                silent += 1
                reason = REASONS.get(outcome.silence) if outcome.silence else "неизвестно"
                print(f"  [{slot.value:7}] молчит — {reason}")
                continue

            sent += 1
            print(f"  [{slot.value:7}] ушло бы:")
            for line in outcome.text.splitlines():
                print(f"      {line}")
        print()

    print(f"Итого: сообщений {sent}, молчаний {silent}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Повтор касаний по живому архиву")
    parser.add_argument(
        "--timezone",
        default=None,
        help="таймзона владельца; по умолчанию BRAIN_TIMEZONE",
    )
    args = parser.parse_args()
    timezone = args.timezone or get_settings().brain_timezone
    asyncio.run(run(timezone))


if __name__ == "__main__":
    main()
