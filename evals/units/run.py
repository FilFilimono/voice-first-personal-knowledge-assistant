"""Сколько единиц архива нечем назвать. Ноль вызовов модели.

    uv run python -m evals.units.run
    uv run python -m evals.units.run --show 20

Считает признак из `evals/units/checks.py` по тому, что **лежит в базе сейчас**:
по смысловым единицам текущей разметки и по снимкам дел. Провайдер не зовётся
ни разу — это запрос к базе, и стоит он ноль.

**Режим, который воспроизводится.** Никакой: прогон ничего не размечает
и не переразмечает, он читает результат работы воркера. Поэтому и правило
«замер обязан воспроизводить рабочий режим» здесь выполняется буквально —
меряется ровно то, что воркер уже сделал.

**Как этим пользоваться.** Число снимается до перепрогона разметки и после
него, на одном и том же архиве:

    1. `uv run python -m evals.units.run`            — до правки промпта
    2. `just reenrich --recompute`                   — перепрогон, ~85×2 вызова
    3. `uv run python -m evals.units.run`            — после

Разница между первым и третьим и есть цена правки границ. Ждать перепрогона
дорого (два прогона на запись, суточный лимит на модель), поэтому шаг 2
планируется отдельным днём — а шаги 1 и 3 бесплатны и повторяемы.
"""

import argparse
import asyncio

from sqlalchemy import func, select

from brain.core.memory.repository import working_text
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.annotation import Annotation
from brain.storage.models.commitment import Commitment
from brain.storage.models.entry import Entry
from brain.storage.models.unit import EntryUnit
from evals.units import checks


async def _units() -> list[tuple[int, str]]:
    """Отрезки текущей разметки, вырезанные из рабочего текста записи.

    «Текущая» — последняя строка `annotations` по записи: перепрогон добавляет
    разметку, а не правит старую, и мерить надо ту, что работает.
    """
    latest = (
        select(Annotation.source_entry_id, func.max(Annotation.id).label("id"))
        .group_by(Annotation.source_entry_id)
        .subquery()
    )
    text_of = working_text()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(EntryUnit.source_entry_id, EntryUnit.span_start, EntryUnit.span_end, text_of)
                .join(latest, latest.c.id == EntryUnit.annotation_id)
                .join(Entry, Entry.id == EntryUnit.source_entry_id)
                .order_by(EntryUnit.source_entry_id, EntryUnit.ordinal)
            )
        ).all()

    return [(int(row[0]), str(row[3] or "")[row[1] : row[2]].strip()) for row in rows]


async def _commitments() -> list[tuple[int, str]]:
    """Снимки дел. Именно они занимают строки утренней сводки."""
    async with session_scope() as session:
        rows = (
            await session.execute(select(Commitment.id, Commitment.text).order_by(Commitment.id))
        ).all()
    return [(int(row[0]), str(row[1])) for row in rows]


def _report(name: str, items: list[tuple[int, str]], show: int) -> None:
    flagged = [(item_id, text, checks.dangling(text)) for item_id, text in items]
    flagged = [row for row in flagged if row[2]]

    total = len(items)
    share = f"{len(flagged) / total:.2f}" if total else "—"
    print(f"\n{name}: {len(flagged)} из {total} ({share})")

    for item_id, text, pronouns in flagged[:show]:
        print(f"  {item_id}: [{', '.join(pronouns)}] {' '.join(text.split())[:90]}")
    if len(flagged) > show:
        print(f"  …и ещё {len(flagged) - show}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="единицы без предмета действия")
    parser.add_argument("--show", type=int, default=10, help="сколько примеров печатать")
    args = parser.parse_args()

    try:
        print("Единица без хозяина местоимения — прибор, а не приговор:")
        print("считается местоимение, слева от которого внутри единицы нет знаменательного слова.")
        _report("Смысловые единицы текущей разметки", await _units(), args.show)
        _report("Дела (снимки)", await _commitments(), args.show)
    finally:
        await dispose_engine()


if __name__ == "__main__":  # pragma: no cover — разовая команда
    asyncio.run(main())
