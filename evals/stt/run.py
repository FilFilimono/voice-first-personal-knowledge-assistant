"""Замер расшифровки. Режим: **по одной записи**, как расшифровывает воркер.

Пачками мерить нельзя, и это правило проекта, а не осторожность: воркер зовёт
расшифровку одним заданием на одну запись (`core/scheduler/jobs.transcribe`),
а провайдер на пачке ведёт себя иначе. Ровно этой ошибкой два дня держался
неверный диагноз про темы (ADR-0011 измерен пачками по 17–25, воркер работал
по одной, разница вышла между одной темой и восемнадцатью).

    just eval-stt --engine gemini
    just eval-stt --engine handy

**Две стороны, один корпус.** Ключ `--engine` выбирает движок: облачный Gemini
через рабочую дверь `core/stt` или локальный Parakeet через Handy. Эталоны,
номера записей и сам звук при этом одни и те же. Иначе это снова сравнение
измеренного с неизмеренным — ошибка, уже стоившая проекту двух дней
на моделях чистки.

**Два числа, и второе главнее.** WER говорит, много ли слов услышано неверно,
и считает все слова равными. Имя — одно слово из сотни, поэтому движок,
перевравший все имена, покажет отличный WER. А ломает систему дальше по цепочке
именно имя: испорченное, оно ложится в `transcripts.text` — таблицу, которая
не правится, — и уезжает в разметку, чанки, поиск, дела и названия. Поэтому
рядом с WER считается доля искажённых имён собственных (`names.py`).

**Корпус адресуется номерами записей, а не файлами.** Звук берётся из
`var/audio` по `entries.audio_path`, эталоны лежат в `references.jsonl` рядом
и в git не уходят: эталон — это дословная речь владельца целиком (ADR-0002).
В репозиторий едет `dataset.jsonl` — номера и число слов.

**Печатает живые тексты.** Иначе искажённые имена нечем читать глазами. Это
терминал владельца, и так задумано; в `results/` ложатся только числа
и номера записей.
"""

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from brain.config import get_settings
from brain.core.memory.repository import is_private_now
from brain.core.ports import Transcriber
from brain.core.scheduler.jobs import VOICE_MIME
from brain.core.stt.errors import TranscriptionError
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.entry import Entry, EntryKind
from evals.stt import engines, names
from evals.stt.wer import Score, corpus_wer, score, words

HERE = Path(__file__).resolve().parent

# Номера записей и числа — это в git.
DATASET = HERE / "dataset.jsonl"

# Эталоны — на диске владельца и только там. Дословная расшифровка это его речь
# целиком, а опубликованную историю git не отозвать (ADR-0002). Перечислен
# в `evals/stt/.gitignore` — так же, как `units.jsonl` в замере чистки.
REFERENCES = HERE / "references.jsonl"

RESULTS = HERE / "results"


@dataclass(frozen=True, slots=True)
class Marked:
    """Строка набора: номер записи, число слов эталона и заметка владельца.

    Число слов лежит рядом с номером не для красоты. Эталон правится руками,
    а WER считается по числу слов эталона — переписав эталон между прогонами,
    владелец сравнил бы движки по разной линейке и не заметил бы этого.
    """

    entry_id: int
    words: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class Case:
    """Одна запись корпуса: номер, файл со звуком и то, что было сказано."""

    entry_id: int
    audio: Path
    reference: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """Что услышал движок и насколько это разошлось с эталоном.

    `provider` и `model` едут в каждом исходе, а не одним полем на прогон:
    отчёт берёт имя движка **из ответов**, и разъехаться настройке с тем,
    что на самом деле отвечало, тогда негде.
    """

    entry_id: int
    reference: str
    hypothesis: str
    score: Score
    names: names.Names
    provider: str
    model: str
    # Время самого движка: у облачного — вокруг HTTP-вызова, у локального —
    # загрузка модели плюс расшифровка.
    latency_ms: int
    # Время прогона вокруг всего: запуск процесса, разворот звука, разбор
    # ответа. У облачного движка совпадает с предыдущим, у локального — нет.
    wall_ms: int
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """Итог прогона вместе с тем, полным ли он был.

    Оборванный прогон обязан быть отличим от полного и в заголовке, и в файле:
    файлы результатов потом сравниваются между собой, и «пять из двадцати»,
    прочитанное как «двадцать», — это и есть сравнение с неизмеренным.
    """

    outcomes: list[Outcome]
    planned: int
    interrupted: str | None = None


def load_dataset(path: Path) -> list[Marked]:
    """Набор из git: номера записей и числа. Ни одной строки речи."""
    if not path.exists():
        raise SystemExit(
            f"Нет набора: {path}\n"
            "Двадцать голосовых наговаривает и размечает владелец — см. evals/stt/README.md."
        )

    marked: list[Marked] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            marked.append(
                Marked(
                    entry_id=int(row["entry_id"]),
                    words=int(row["words"]),
                    note=str(row.get("note", "")),
                )
            )
        except (ValueError, KeyError, TypeError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None

    if not marked:
        raise SystemExit(f"{path}: набор пуст — размечать его владельцу, см. README.md")
    return marked


def load_references(path: Path) -> dict[int, str]:
    """Эталоны с диска владельца. Пустой файл — законное состояние, но мерить нечего."""
    if not path.exists():
        return {}

    references: dict[int, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            references[int(row["entry_id"])] = str(row["reference"])
        except (ValueError, KeyError, TypeError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None
    return references


async def audio_paths(entry_ids: list[int]) -> dict[int, str]:
    """Где лежит звук этих записей.

    Голосовые и неприватные — инвариант 5. Формально приватного голосового
    быть не может (голос в приватном режиме не принимается), но приватность
    включается **задним числом**, и тогда дверь `core/stt` подняла бы
    исключение на середине прогона.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, Entry.audio_path).where(
                    Entry.id.in_(entry_ids),
                    Entry.kind == EntryKind.VOICE,
                    Entry.audio_path.is_not(None),
                    ~is_private_now(),
                )
            )
        ).all()
    return {int(row[0]): str(row[1]) for row in rows}


def build_cases(
    marked: list[Marked], references: dict[int, str], audio: dict[int, str]
) -> list[Case]:
    """Сложить корпус из трёх частей: номера, эталона и файла.

    Расхождения здесь — не мелочь, поэтому они останавливают прогон, а не
    печатаются предупреждением: эталон, потерявшийся молча, занизил бы WER
    корпуса на целую запись, а переписанный — сделал бы два прогона
    несравнимыми. Исключение одно, и оно ниже.
    """
    missing = [item.entry_id for item in marked if item.entry_id not in references]
    if missing:
        raise SystemExit(
            f"Нет эталона для записей: {missing}\n"
            f"Эталоны лежат в {REFERENCES.name} рядом с набором (в git он не уходит).\n"
            "Как размечать — evals/stt/README.md."
        )

    drifted = [
        f"запись {item.entry_id}: в наборе {item.words} слов, в эталоне "
        f"{len(words(references[item.entry_id]))}"
        for item in marked
        if item.words != len(words(references[item.entry_id]))
    ]
    if drifted:
        raise SystemExit(
            "Разметка разъехалась — эталон правили после того, как записали число слов:\n  "
            + "\n  ".join(drifted)
            + "\nWER считается по числу слов эталона: два прогона с разными эталонами\n"
            "сравнивать нельзя. Поправьте `words` в dataset.jsonl и перемерьте обе стороны."
        )

    cases: list[Case] = []
    for item in marked:
        # Единственное расхождение, которое прогон переживает: звука нет.
        # Он и не обязан быть — ретеншен удаляет файлы через тридцать дней,
        # а строка записи остаётся. Прогон об этом говорит и продолжает,
        # но число «задумано» в отчёте остаётся прежним.
        path = audio.get(item.entry_id)
        if path is None or not Path(path).exists():
            print(
                f"⚠️  Запись {item.entry_id}: звука нет. Либо её нет в архиве, либо это"
                " не голосовое,\n    либо она приватная, либо аудио удалено ретеншеном"
                " (тридцать дней)."
            )
            continue
        cases.append(
            Case(entry_id=item.entry_id, audio=Path(path), reference=references[item.entry_id])
        )

    return cases


def judge(
    case: Case,
    hypothesis: str,
    *,
    provider: str,
    model: str,
    latency_ms: int = 0,
    wall_ms: int = 0,
) -> Outcome:
    """Сравнить услышанное с эталоном. Чистая функция — её гоняют быстрые тесты."""
    return Outcome(
        entry_id=case.entry_id,
        reference=case.reference,
        hypothesis=hypothesis,
        score=score(case.reference, hypothesis),
        names=names.compare(case.reference, hypothesis),
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        wall_ms=wall_ms,
    )


async def transcribe_all(cases: list[Case], *, transcriber: Transcriber) -> Run:
    """Расшифровать корпус **по одной записи**, замеряя каждый вызов отдельно.

    Последовательно, а не пачкой, и это не про лимиты провайдера: воркер
    работает именно так, а замер в нерабочем режиме подтверждает представления
    автора, а не работу системы.
    """
    outcomes: list[Outcome] = []

    for case in cases:
        started = time.perf_counter()
        try:
            transcription = await transcriber.transcribe(case.audio.read_bytes(), VOICE_MIME)
        except (TranscriptionError, OSError) as error:
            outcomes.append(
                Outcome(
                    entry_id=case.entry_id,
                    reference=case.reference,
                    hypothesis="",
                    score=Score(errors=0, reference_words=0),
                    names=names.Names(),
                    provider=transcriber.provider,
                    model=transcriber.model,
                    latency_ms=0,
                    wall_ms=int((time.perf_counter() - started) * 1000),
                    failure=f"{type(error).__name__}: {error}"[:200],
                )
            )
            continue

        outcomes.append(
            judge(
                case,
                transcription.text,
                provider=transcription.provider,
                model=transcription.model,
                latency_ms=transcription.latency_ms,
                wall_ms=int((time.perf_counter() - started) * 1000),
            )
        )

    return Run(outcomes=outcomes, planned=len(cases))


def _one(values: list[str], *, unknown: str) -> str:
    """Одно значение на весь прогон — или все, если движок ответил по-разному.

    Разные модели внутри одного прогона это не мелочь: файл результата
    подписывается именем движка, и «одна из двух» обязана быть видна.
    """
    seen = sorted(set(values))
    if not seen:
        return unknown
    return seen[0] if len(seen) == 1 else " и ".join(seen)


def report(outcomes: list[Outcome], *, planned: int, interrupted: str | None) -> dict[str, object]:
    """Напечатать результат и вернуть числа — для файла.

    В возвращаемом словаре нет ни эталонов, ни расшифровок, ни имён файлов:
    `results/` лежит в репозитории, эталон — это речь владельца целиком,
    а в имени аудиофайла его `chat_id`. В git едут номера и числа (ADR-0002).
    """
    heard = [outcome for outcome in outcomes if outcome.failure is None]
    engine = _one([outcome.provider for outcome in heard], unknown="—")
    model = _one([outcome.model for outcome in heard], unknown="—")

    counted = f"{len(outcomes)}" if len(outcomes) == planned else f"{len(outcomes)} из {planned}"
    print(f"\nЗаписей: {counted}   движок: {engine}   модель: {model}   режим: по одной записи")
    if interrupted:
        print(f"⚠️  Прогон неполон: {interrupted}. Числа ниже — по тому, что прошло.")

    total_wer = corpus_wer([outcome.score for outcome in heard])
    reports = [outcome.names for outcome in heard]
    expected_names = sum(len(item.expected) for item in reports)
    distorted_names = sum(len(item.distorted) for item in reports)
    share = names.corpus_share(reports)

    print("\nПо записям:")
    for outcome in outcomes:
        if outcome.failure is not None:
            print(f"  запись {outcome.entry_id:<4} — не расшифрована: {outcome.failure}")
            continue
        broken = len(outcome.names.distorted)
        mark = f"  имена {broken}/{len(outcome.names.expected)}" if outcome.names.expected else ""
        print(
            f"  запись {outcome.entry_id:<4} WER {outcome.score.wer:6.1%}"
            f"  {outcome.latency_ms / 1000:5.1f} с{mark}"
        )

    print(f"\nWER по корпусу: {total_wer:.1%}")
    print(f"Искажено имён: {share:.1%} ({distorted_names} из {expected_names})")
    if expected_names == 0:
        print("  ⚠️  В эталонах нет ни одного имени собственного. Либо корпус их не содержит,")
        print("      либо имена записаны строчными — тогда признак их не видит, см. README.md.")

    # **Это и есть ответ на вопрос, ради которого замер написан.** Общий WER
    # может быть отличным при полностью испорченных именах, поэтому список
    # печатается всегда и целиком: его читают глазами.
    broken_ones = [outcome for outcome in heard if outcome.names.distorted]
    if broken_ones:
        print("\nИскажённые имена — читать глазами:")
        for outcome in broken_ones:
            print(f"  запись {outcome.entry_id}: не услышано {', '.join(outcome.names.distorted)}")
            print(f"     сказано:  {outcome.reference}")
            print(f"     услышано: {outcome.hypothesis}")
    else:
        print("\nИскажённых имён нет.")

    latencies = [outcome.latency_ms for outcome in heard]
    walls = [outcome.wall_ms for outcome in heard]
    median = int(statistics.median(latencies)) if latencies else 0
    print("\nЛатентность одного вызова (время движка):")
    print(f"  медиана   {median / 1000:.1f} с")
    print(f"  максимум  {max(latencies, default=0) / 1000:.1f} с")
    print(
        f"  вокруг вызова целиком: медиана"
        f" {int(statistics.median(walls)) / 1000 if walls else 0:.1f} с"
    )
    print(
        "  Разница между двумя строками — накладные расходы **замера**, а не движка:\n"
        "  запуск процесса и разворот звука. У облачного движка их нет."
    )

    failures = [outcome for outcome in outcomes if outcome.failure is not None]
    return {
        "режим": "по одной записи",
        "движок": engine,
        "модель": model,
        "записей": len(outcomes),
        "задумано": planned,
        "оборван": interrupted,
        "расшифровано": len(heard),
        "wer": round(total_wer, 4),
        "искажено имён": round(share, 4),
        "имён в эталоне": expected_names,
        "имён искажено штук": distorted_names,
        # Номера, а не тексты: по ним запись открывается в терминале, а в git
        # не уезжает ни слова.
        "записи с искажёнными именами": [outcome.entry_id for outcome in broken_ones],
        "не расшифровано": [outcome.entry_id for outcome in failures],
        "латентность мс": {
            "медиана": median,
            "максимум": max(latencies, default=0),
            "вокруг вызова медиана": int(statistics.median(walls)) if walls else 0,
        },
        "по записям": [
            {
                "запись": outcome.entry_id,
                "wer": round(outcome.score.wer, 4),
                "слов в эталоне": outcome.score.reference_words,
                "имён": len(outcome.names.expected),
                "искажено": len(outcome.names.distorted),
                "мс": outcome.latency_ms,
            }
            for outcome in heard
        ],
    }


def save(summary: dict[str, object], *, stamp: datetime) -> Path:
    """Сохранить прогон. Только числа и номера записей — текстов здесь нет."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{stamp:%Y%m%d_%H%M}_{summary['движок']}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Замер расшифровки. Режим: по одной записи, как расшифровывает воркер.",
        epilog=(
            "Две стороны одного корпуса: --engine gemini и --engine handy. "
            "Мерить надо обе, иначе это сравнение измеренного с неизмеренным."
        ),
    )
    # Без значения по умолчанию намеренно: движок обязан быть назван вслух.
    # Умолчание однажды дало бы прогон, у которого сторону не заметили.
    parser.add_argument("--engine", required=True, help=f"движок: {' или '.join(engines.ENGINES)}")
    parser.add_argument("--model", default=None, help="имя модели вместо той, что в настройках")
    parser.add_argument("--handy", type=Path, default=None, help="путь к приложению Handy")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--references", type=Path, default=REFERENCES)
    arguments = parser.parse_args()

    # `get_settings`, а не `get_runtime_settings`: локальная сторона замера
    # не ходит в сеть, и требовать от неё токен бота значило бы запретить
    # прогон ровно там, где он ничего не стоит.
    settings = get_settings()
    stamp = datetime.now(UTC)

    try:
        marked = load_dataset(arguments.dataset)
        references = load_references(arguments.references)
        if not references:
            raise SystemExit(
                f"Нет эталонов: {arguments.references}\n"
                "Без них мерить нечего. Эталон пишет владелец дословно —"
                " см. evals/stt/README.md."
            )

        transcriber = engines.build(
            arguments.engine, settings, model=arguments.model, binary=arguments.handy
        )
        cases = build_cases(marked, references, await audio_paths([m.entry_id for m in marked]))
        if not cases:
            raise SystemExit("Ни одной записи со звуком — мерить нечего.")

        finished = await transcribe_all(cases, transcriber=transcriber)
        summary = report(
            finished.outcomes,
            planned=len(marked),
            interrupted=(
                None
                if len(finished.outcomes) == len(marked)
                else f"звука нет у {len(marked) - len(finished.outcomes)} записей"
            ),
        )
        print(f"\nЗаписано: {save(summary, stamp=stamp)}")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
