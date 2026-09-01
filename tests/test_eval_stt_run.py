"""Обвязка замера расшифровки: чем меряем, что печатаем и что уезжает в git.

Проверок здесь четыре сорта, и все четыре — про то, как замер **врёт** или
**вредит**, если ошибиться.

1. **Чем меряем.** Замер сравнивает два движка, значит движок выбирается ключом,
   а имя движка в отчёте берётся **из ответов**, а не из настройки рядом.
   Разъехаться им иначе негде — и ровно так замер чистки один раз уже мерил бы
   разметочную модель, подписывая её именем чистящей (`evals/cleanup/run.py`,
   `build_cleaner`).
2. **Сколько померили.** Прогон, не дошедший до конца, обязан быть отличим
   от полного и в заголовке, и в файле: файлы потом сравниваются между собой.
   «Сравнили измеренное с неизмеренным» — ошибка, уже стоившая проекту двух
   дней на моделях чистки.
3. **В каком режиме.** Воркер расшифровывает **по одной записи**. Замер,
   зовущий провайдера пачкой, меряет другой режим и подписывает результат
   именем рабочего.
4. **Что уезжает в git.** Здесь это тяжелее, чем в остальных замерах: эталон
   пишет владелец **дословно**, то есть это его речь целиком, а имя аудиофайла
   содержит его `chat_id`. Ни то, ни другое в репозиторий попасть не может
   (ADR-0002), а `results/` лежит в репозитории.

Живого вызова тут нет — ни сети, ни Handy: движки подменяются заглушками.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from brain.config import Settings
from brain.core.ports import Transcription
from evals.stt import engines, run
from tests.archive import Entry, entry

# 1, голос. Содержит «CV» — то самое слово, на котором движки разошлись живьём.
CV_VOICE = 1


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


class SoloTranscriber:
    """Движок, который следит, чтобы его звали **по одной записи**.

    Не «принимает список и разбирается сам»: параллельный вызов провайдера —
    другой режим, и результат такого замера нельзя подписывать именем рабочего.
    """

    def __init__(self, text: str = "услышано", *, provider: str = "stub") -> None:
        self._text = text
        self._provider = provider
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return "stub-model"

    async def check_available(self) -> None:
        raise AssertionError("замер не проверяет доступность — он просто зовёт")

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return Transcription(
                text=self._text,
                provider=self._provider,
                model="stub-model",
                latency_ms=100,
            )
        finally:
            self.in_flight -= 1


def case(tmp_path: Path, entry_id: int, reference: str) -> run.Case:
    audio = tmp_path / f"{entry_id}.oga"
    # Звук не настоящий: движок подменён заглушкой, читается только длина файла.
    audio.write_bytes("не настоящий звук".encode())
    return run.Case(entry_id=entry_id, audio=audio, reference=reference)


# --- Чем меряем ---


def test_engine_key_chooses_the_local_engine() -> None:
    """`--engine` выбирает движок, и локальный не требует ни ключа, ни сети."""
    local = engines.build(engines.HANDY, settings(google_api_key=""))

    assert local.provider == engines.HANDY


def test_engine_key_chooses_gemini() -> None:
    """Вторая сторона того же корпуса — рабочая дверь `core/stt`."""
    remote = engines.build(engines.GEMINI, settings(google_api_key="ключ"))

    assert remote.provider == "gemini"


def test_gemini_without_key_is_refused_before_the_corpus_is_read() -> None:
    """Пустой ключ — внятная строка, а не отказ провайдера на первой записи."""
    with pytest.raises(SystemExit) as failure:
        engines.build(engines.GEMINI, settings(google_api_key=""))

    assert "GOOGLE_API_KEY" in str(failure.value)


def test_unknown_engine_lists_the_known_ones() -> None:
    """Опечатка в ключе не должна выглядеть как отсутствующий движок."""
    with pytest.raises(SystemExit) as failure:
        engines.build("parakeet", settings())

    assert engines.HANDY in str(failure.value)
    assert engines.GEMINI in str(failure.value)


# --- В каком режиме ---


async def test_corpus_is_transcribed_one_at_a_time(tmp_path: Path) -> None:
    """Режим замера — тот же, в котором работает воркер: по одной записи."""
    transcriber = SoloTranscriber()
    cases = [case(tmp_path, number, "эталон") for number in (1, 2, 3)]

    finished = await run.transcribe_all(cases, transcriber=transcriber)

    assert transcriber.calls == 3
    assert transcriber.max_in_flight == 1
    assert len(finished.outcomes) == 3


# --- Сколько померили ---


def test_partial_corpus_is_visible_in_the_report(tmp_path: Path) -> None:
    """Пять записей из двадцати не имеют права выглядеть как двадцать."""
    outcomes = [run.judge(case(tmp_path, 1, "эталон"), "эталон", provider="stub", model="m")]

    summary = run.report(outcomes, planned=20, interrupted="аудио не найдено")

    assert summary["записей"] == 1
    assert summary["задумано"] == 20
    assert summary["оборван"] == "аудио не найдено"


def test_engine_name_comes_from_the_answers(tmp_path: Path) -> None:
    """Имя движка в отчёте — из ответов, а не из ключа командной строки.

    Разойтись им тогда негде. Настройка рядом с результатом однажды уже
    подписала бы чужую модель именем измеряемой (`evals/cleanup`).
    """
    outcomes = [
        run.judge(case(tmp_path, 1, "эталон"), "эталон", provider="handy", model="parakeet-v3")
    ]

    summary = run.report(outcomes, planned=1, interrupted=None)

    assert summary["движок"] == "handy"
    assert summary["модель"] == "parakeet-v3"


# --- Что уезжает в git ---


def test_results_carry_no_speech(tmp_path: Path, archive: list[Entry]) -> None:
    """В файл результата не попадает ни эталон, ни расшифровка, ни имя файла.

    `results/` лежит рядом с кодом, а эталон — это дословная речь владельца
    целиком. Имя аудиофайла тоже не годится: в нём `chat_id`. В git едут
    номера записей и числа (ADR-0002).
    """
    reference = entry(archive, CV_VOICE).text
    outcomes = [
        run.judge(
            case(tmp_path, CV_VOICE, reference),
            "услышано совсем не то",
            provider="stub",
            model="m",
        )
    ]

    written = json.dumps(run.report(outcomes, planned=1, interrupted=None), ensure_ascii=False)

    assert reference[:40] not in written
    assert "услышано совсем не то" not in written
    assert f"{CV_VOICE}.oga" not in written


def test_names_share_is_reported_next_to_wer(tmp_path: Path, archive: list[Entry]) -> None:
    """Доля искажённых имён — головное число замера наравне с WER."""
    reference = entry(archive, CV_VOICE).text
    outcomes = [
        run.judge(
            case(tmp_path, CV_VOICE, reference),
            reference.replace("CV", "Сиви"),
            provider="stub",
            model="m",
        )
    ]

    summary = run.report(outcomes, planned=1, interrupted=None)

    assert summary["искажено имён"] == 1.0
    assert summary["имён в эталоне"] == 1


# --- Формат корпуса ---


def test_corpus_is_addressed_by_entry_number(tmp_path: Path) -> None:
    """Набор в git — номера записей и числа, без единой строки речи."""
    path = tmp_path / "dataset.jsonl"
    path.write_text(
        '// комментарий\n{"entry_id": 7, "words": 42, "note": "на улице"}\n',
        encoding="utf-8",
    )

    marked = run.load_dataset(path)

    assert marked == [run.Marked(entry_id=7, words=42, note="на улице")]


def test_missing_reference_is_named_by_entry(tmp_path: Path) -> None:
    """Запись без эталона молча не пропускается: она бы занизила WER корпуса."""
    marked = [run.Marked(entry_id=7, words=42, note="")]

    with pytest.raises(SystemExit) as failure:
        run.build_cases(marked, references={}, audio={7: "var/audio/7.oga"})

    assert "7" in str(failure.value)


def test_marked_word_count_catches_a_changed_reference(tmp_path: Path) -> None:
    """Эталон, переписанный между прогонами, делает два прогона несравнимыми.

    Число слов лежит в git рядом с номером именно затем, чтобы это было видно:
    иначе разница между движками покажет правку разметки, а не движок.
    """
    marked = [run.Marked(entry_id=7, words=2, note="")]

    with pytest.raises(SystemExit) as failure:
        run.build_cases(
            marked,
            references={7: "здесь на самом деле пять слов, а не два"},
            audio={7: "var/audio/7.oga"},
        )

    assert "разъехалась" in str(failure.value)
