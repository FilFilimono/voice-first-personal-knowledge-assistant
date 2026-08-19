"""Прогон eval классификации.

    uv run python -m evals.classification.run
    uv run python -m evals.classification.run --model gemini-3.1-flash-lite

Ходит в тот же `core/enrich`, что и воркер: тем же промптом, той же схемой
ответа, тем же правилом состояния. Мерить копию разметки бессмысленно —
разойдясь на одну строку, она перестанет мерить прод.

**Что печатается.** Точность — число, без которого Фаза 3 не закрыта. Матрица
ошибок — то, чем правится промпт: она показывает, в какую сторону модель
путается, а не только как часто. И доля ошибок среди «уверен» — проверка того,
что согласие двух прогонов вообще что-то значит.

**Чего этот прогон не делает.** Он ничего не пишет в `annotations`: набор
гоняется по многу раз и по разным моделям, и архив от этого замусориваться
не должен. Строку в `llm_calls` он пишет — прогон стоит денег, и они должны
быть видны там же, где остальные.

Тексты берутся из живой базы по номерам записей. В наборе лежат только номера
и типы: архив в git не уходит (ADR-0002).
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from brain.config import get_runtime_settings
from brain.core import llm
from brain.core.enrich import prompt, repository, service
from brain.core.memory.repository import latest_transcript
from brain.core.ports import EntryText
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.entry import Entry
from evals.classification.metrics import (
    Judged,
    Labelled,
    accuracy,
    accuracy_within_two,
    by_state,
    confusion,
    error_rate_when_sure,
)

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"
RESULTS = HERE / "results"


def load_dataset(path: Path) -> list[Labelled]:
    if not path.exists():
        raise SystemExit(
            f"Нет набора: {path}\n"
            "Сто записей размечает владелец — см. evals/classification/README.md."
        )

    labelled: list[Labelled] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            labelled.append(
                Labelled(
                    entry_id=int(row["entry_id"]),
                    expected=AnnotationType(row["type"]),
                    note=row.get("note", ""),
                )
            )
        except (ValueError, KeyError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None

    if not labelled:
        raise SystemExit(f"Набор пуст: {path}")
    return labelled


async def texts_of(entry_ids: list[int]) -> dict[int, str]:
    """Тексты записей из живой базы. Их нет в репозитории и не должно быть."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, func.coalesce(latest_transcript(), Entry.raw_text)).where(
                    Entry.id.in_(entry_ids)
                )
            )
        ).all()
    return {int(row[0]): str(row[1]) for row in rows if row[1]}


async def judge(labelled: list[Labelled], *, model_name: str | None, batch: int) -> list[Judged]:
    """Разметить набор и сравнить с разметкой владельца."""
    # Сравнить две модели на одном наборе — главное, ради чего этот прогон
    # запускается второй раз (ADR-0011: выбор подтверждается замером, не прайсом).
    model = llm.build_model(get_runtime_settings(), model=model_name)

    texts = await texts_of([item.entry_id for item in labelled])
    missing = [item.entry_id for item in labelled if item.entry_id not in texts]
    if missing:
        print(f"⚠️  Нет в базе: {missing} — разметка ссылается на записи, которых нет")

    known = await repository.known_topics()
    system = prompt.build_system([topic.name for topic in known])

    judged: list[Judged] = []
    for start in range(0, len(labelled), batch):
        chunk = [item for item in labelled[start : start + batch] if item.entry_id in texts]
        if not chunk:
            continue

        entries = [
            EntryText(entry_id=item.entry_id, text=texts[item.entry_id], is_private=False)
            for item in chunk
        ]
        runs = [
            service._parse(
                (
                    await llm.complete_over_entries(
                        model,
                        entries=entries,
                        system=system,
                        schema=prompt.response_schema(),
                        temperature=service.TEMPERATURE,
                        purpose="eval-classification",
                    )
                ).data,
                {entry.entry_id: entry.text for entry in entries},
            )
            for _ in range(service.RUNS)
        ]

        for item in chunk:
            merged = service._merge([run.get(item.entry_id) for run in runs], texts[item.entry_id])
            judged.append(
                Judged(
                    entry_id=item.entry_id,
                    expected=item.expected,
                    got=merged.type,
                    alternative=merged.alternative_type,
                    state=merged.state,
                )
            )
    return judged


def report(judged: list[Judged], *, model_name: str) -> dict[str, object]:
    """Напечатать результат и вернуть его же — для файла."""
    print(f"Записей: {len(judged)}   модель: {model_name}")
    print(f"точность:                   {accuracy(judged):.2f}")
    print(f"точность с учётом второй:   {accuracy_within_two(judged):.2f}")
    print(f"ошибок там, где «уверен»:   {error_rate_when_sure(judged):.2f}")

    print("\nСостояния:")
    for state, count in by_state(judged).most_common():
        print(f"  {state:<16} {count}")

    errors = confusion(judged)
    if errors:
        print("\nОшибки (ждали → получили):")
        for (expected, got), count in errors.most_common():
            print(f"  {expected:<10} → {got:<10} {count}")
        print("\nЧитать глазами:")
        for item in judged:
            if not item.correct:
                got = item.got.value if item.got else "—"
                print(f"  запись {item.entry_id}: ждали {item.expected.value}, вышло {got}")
    else:
        print("\nОшибок нет.")

    return {
        "записей": len(judged),
        "модель": model_name,
        "точность": round(accuracy(judged), 4),
        "точность с учётом второй": round(accuracy_within_two(judged), 4),
        "ошибок среди уверенных": round(error_rate_when_sure(judged), 4),
        "состояния": dict(by_state(judged)),
        "ошибки": {f"{a} → {b}": n for (a, b), n in confusion(judged).items()},
    }


def save(summary: dict[str, object], *, stamp: datetime) -> Path:
    """Сохранить прогон. Ухудшение — блокер мержа."""
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{stamp:%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон eval классификации")
    parser.add_argument("--model", default=None, help="имя модели вместо ENRICH_MODEL")
    parser.add_argument("--batch", type=int, default=25, help="записей в одном вызове")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    arguments = parser.parse_args()

    labelled = load_dataset(arguments.dataset)
    name = arguments.model or get_runtime_settings().enrich_model
    try:
        judged = await judge(labelled, model_name=arguments.model, batch=arguments.batch)
        summary = report(judged, model_name=name)
        print(f"\nЗаписано: {save(summary, stamp=datetime.now(UTC))}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
