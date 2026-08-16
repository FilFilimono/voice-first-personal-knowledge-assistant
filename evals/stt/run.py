"""Прогон eval-набора расшифровки.

    uv run python -m evals.stt.run
    uv run python -m evals.stt.run --model gemini-2.5-flash

Ходит в того же провайдера, что и бот, через тот же `core/stt` — иначе
мерился бы не тот код, который работает в проде.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from brain.config import get_settings
from brain.core import stt
from evals.stt.wer import Score, corpus_wer, score

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"
RESULTS = HERE / "results"

MIME = "audio/ogg"


@dataclass(frozen=True, slots=True)
class Case:
    """Одна запись корпуса: файл и то, что было сказано на самом деле."""

    audio: Path
    reference: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """Что услышал провайдер и насколько это разошлось с эталоном."""

    case: Case
    hypothesis: str
    score: Score
    latency_ms: int


def load_dataset(path: Path) -> list[Case]:
    if not path.exists():
        raise SystemExit(
            f"Нет корпуса: {path}\n"
            "Двадцать голосовых наговаривает владелец — см. evals/stt/README.md."
        )

    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        audio = HERE / record["audio"]
        if not audio.exists():
            raise SystemExit(f"{path}:{number}: нет файла {audio}")
        cases.append(Case(audio=audio, reference=record["reference"]))

    if not cases:
        raise SystemExit(f"{path}: корпус пуст")
    return cases


async def run_case(transcriber: stt.Transcriber, case: Case) -> Outcome:
    transcription = await transcriber.transcribe(case.audio.read_bytes(), MIME)
    return Outcome(
        case=case,
        hypothesis=transcription.text,
        score=score(case.reference, transcription.text),
        latency_ms=transcription.latency_ms,
    )


def report(outcomes: list[Outcome], *, provider: str, model: str) -> dict[str, object]:
    print(f"\nПровайдер: {provider} · модель: {model}\n")
    for outcome in outcomes:
        name = outcome.case.audio.name
        print(f"  {name:<24} WER {outcome.score.wer:6.1%}  {outcome.latency_ms:>6} мс")

    total = corpus_wer([outcome.score for outcome in outcomes])
    print(f"\n  Всего записей: {len(outcomes)}")
    print(f"  WER по корпусу: {total:.1%}\n")

    return {
        "provider": provider,
        "model": model,
        # Время проставляется здесь, а не внутри: результат сравнивают
        # с предыдущими прогонами, и дата — часть результата.
        "run_at": datetime.now(UTC).isoformat(),
        "corpus_wer": total,
        "cases": [
            {
                "audio": outcome.case.audio.name,
                "reference": outcome.case.reference,
                "hypothesis": outcome.hypothesis,
                "wer": outcome.score.wer,
                "latency_ms": outcome.latency_ms,
            }
            for outcome in outcomes
        ],
    }


def save(result: dict[str, object]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = RESULTS / f"{stamp}_{result['model']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон eval-набора STT")
    parser.add_argument("--model", help="переопределить модель из .env")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    arguments = parser.parse_args()

    settings = get_settings()
    if arguments.model:
        settings = settings.model_copy(update={"stt_model": arguments.model})

    cases = load_dataset(arguments.dataset)
    transcriber = stt.build_transcriber(settings)

    # Последовательно, а не пачкой: двадцать записей меряются за минуту,
    # а параллельные запросы упираются в лимиты провайдера и портят латентность.
    outcomes = [await run_case(transcriber, case) for case in cases]

    saved = save(report(outcomes, provider=transcriber.provider, model=transcriber.model))
    print(f"  Результат: {saved}\n")


if __name__ == "__main__":
    asyncio.run(main())
