"""Прогон eval-набора ретривала.

    uv run python -m evals.retrieval.run
    uv run python -m evals.retrieval.run --k 10

Ходит в тот же `core/retrieval`, что и бот. Мерить копию поискового кода
бессмысленно: разойдясь на одну строку, она перестанет мерить прод.

**Что печатается и почему именно это.**

Общие recall@5 и MRR — это число, без которого Фаза 2 не закрыта. Поканальные
recall@5 — это то, чем правятся веса RRF: они начаты равными, и без ответа
на вопрос «какой канал вообще что-то нашёл» любая правка веса была бы
предпочтением, а не выводом. Канал, у которого recall близок к нулю на всём
наборе, свой вес не отрабатывает — и это видно только так.

Отдельной строкой печатаются вопросы, на которые не ответил никто: их стоит
читать глазами. Обычно это либо дыра в поиске, либо ошибка в разметке,
и отличить одно от другого может только владелец.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import brain.core.retrieval.embeddings as embeddings
from brain.config import get_runtime_settings
from brain.core.retrieval import search
from brain.storage.db import dispose_engine
from evals.retrieval.metrics import Question, mean, recall_at, reciprocal_rank

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"
RESULTS = HERE / "results"


@dataclass(frozen=True, slots=True)
class Outcome:
    """Что поиск ответил на один вопрос."""

    question: Question
    found: list[int]
    by_channel: dict[str, list[int]]
    recall: float
    rank: float


def load_dataset(path: Path) -> list[Question]:
    if not path.exists():
        raise SystemExit(
            f"Нет набора: {path}\n"
            "Пятьдесят вопросов размечает владелец — см. evals/retrieval/README.md."
        )

    questions: list[Question] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            questions.append(
                Question(
                    question=row["question"],
                    relevant=tuple(row["relevant_entry_ids"]),
                    note=row.get("note", ""),
                )
            )
        except (ValueError, KeyError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None

    if not questions:
        raise SystemExit(f"Набор пуст: {path}")
    return questions


async def run(questions: list[Question], *, k: int) -> list[Outcome]:
    settings = get_runtime_settings()
    embedder = embeddings.build_embedder(settings)

    outcomes: list[Outcome] = []
    for question in questions:
        by_channel = await search.channel_rankings(embedder, question.question)
        found = search.merge(by_channel)
        outcomes.append(
            Outcome(
                question=question,
                found=found[:k],
                by_channel=by_channel,
                recall=recall_at(found, question.relevant, k),
                rank=reciprocal_rank(found, question.relevant),
            )
        )
    return outcomes


def report(outcomes: list[Outcome], *, k: int) -> dict[str, object]:
    """Напечатать результат и вернуть его же — для файла."""
    recall = mean([outcome.recall for outcome in outcomes])
    rank = mean([outcome.rank for outcome in outcomes])

    print(f"Вопросов: {len(outcomes)}")
    print(f"recall@{k}: {recall:.2f}")
    print(f"MRR:       {rank:.2f}")

    print(f"\nПо каналам (recall@{k} — что нашёл бы канал в одиночку):")
    by_channel: dict[str, float] = {}
    for channel in search.CHANNELS:
        value = mean(
            [
                recall_at(outcome.by_channel[channel], outcome.question.relevant, k)
                for outcome in outcomes
            ]
        )
        by_channel[channel] = value
        print(f"  {channel:<10} {value:.2f}   вес {search.WEIGHTS[channel]}")

    missed = [outcome for outcome in outcomes if outcome.recall == 0.0]
    if missed:
        print(f"\nНе нашлось ({len(missed)}) — читать глазами:")
        for outcome in missed:
            print(f"  «{outcome.question.question}» → ждали {list(outcome.question.relevant)}")
            print(f"     вернулось: {outcome.found}")
    else:
        print("\nНашлось всё.")

    return {
        "вопросов": len(outcomes),
        f"recall@{k}": round(recall, 4),
        "MRR": round(rank, 4),
        "по каналам": {name: round(value, 4) for name, value in by_channel.items()},
        "веса": dict(search.WEIGHTS),
        "не нашлось": [outcome.question.question for outcome in missed],
    }


def save(summary: dict[str, object], *, stamp: datetime) -> Path:
    """Сохранить прогон. Результат каждого прогона записывается — ухудшение это блокер."""
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{stamp:%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон eval-набора ретривала")
    parser.add_argument("--k", type=int, default=5, help="глубина выдачи (по умолчанию 5)")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    arguments = parser.parse_args()

    questions = load_dataset(arguments.dataset)
    try:
        outcomes = await run(questions, k=arguments.k)
        summary = report(outcomes, k=arguments.k)
        print(f"\nЗаписано: {save(summary, stamp=datetime.now(UTC))}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
