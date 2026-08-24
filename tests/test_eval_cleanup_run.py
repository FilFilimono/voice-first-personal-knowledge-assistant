"""Обвязка замера чистки: чем меряем и что печатаем.

Проверок здесь три сорта, и все три — про то, как замер **врёт**, если
ошибиться. Живого вызова тут нет: он делается владельцем.

1. **Чем меряем.** Воркер чистит `CLEANUP_MODEL`, а `llm.build_model` при
   пустом аргументе подставляет `ENRICH_MODEL` — то есть замер по умолчанию
   мерил бы разметочную модель и подписывал её именем чистящей. Оба решения,
   ради которых замер написан, снимались бы с неверного прогона.
2. **Сколько померили.** Оборванный квотой прогон обязан быть отличим
   от полного и в заголовке, и в файле результата: файлы потом сравниваются.
3. **Чего стоит p95.** На десятке точек перцентиль без интерполяции — это
   ровно максимум, и печатать его так же уверенно, как на семидесяти, нельзя.
"""

from collections.abc import Mapping
from typing import Any

from brain.config import Settings
from brain.core.ports import Completion
from evals.cleanup import run
from evals.cleanup.checks import compare


class FakeCleaner:
    """Модель, у которой важно только имя: сеть здесь не нужна."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return "cheap"

    async def check_available(self) -> None:
        raise AssertionError("отчёту хватает имени модели — вызывать её он не должен")

    async def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any], temperature: float
    ) -> Completion:
        raise AssertionError("отчёту хватает имени модели — вызывать её он не должен")


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def outcome(entry_id: int, *, latency_ms: int = 1000, clean: str | None = "Надо.") -> run.Outcome:
    verbatim = "ну надо"
    return run.Outcome(
        entry_id=entry_id,
        verbatim=verbatim,
        clean=clean,
        report=compare(verbatim, clean) if clean is not None else None,
        latency_ms=latency_ms,
    )


def test_measures_the_model_the_worker_cleans_with() -> None:
    """Без ключа меряется `CLEANUP_MODEL`, а не `ENRICH_MODEL`."""
    chosen = run.build_cleaner(
        settings(cleanup_model="cleanup-model", enrich_model="enrich-model"), None
    )

    assert chosen.model == "cleanup-model"


def test_key_overrides_the_setting() -> None:
    """Ради этого ключа замер и запускается второй раз — сравнить модели."""
    chosen = run.build_cleaner(settings(cleanup_model="cleanup-model"), "другая-модель")

    assert chosen.model == "другая-модель"


def test_report_takes_the_name_from_the_built_model() -> None:
    """Имя в отчёте — из модели, которая правда чистила, а не из настройки рядом.

    Настройка и построенная модель разошлись бы молча, и файл результата
    противоречил бы строке в `llm_calls`, где имя настоящее.
    """
    summary = run.report(
        [outcome(1)], model=FakeCleaner("та-самая"), wait_sec=30, planned=1, interrupted=None
    )

    assert summary["модель"] == "та-самая"


def test_partial_run_is_distinguishable_from_a_full_one() -> None:
    summary = run.report(
        [outcome(1), outcome(2)],
        model=FakeCleaner("модель"),
        wait_sec=30,
        planned=70,
        interrupted="квота (429) на записи 3",
    )

    assert summary["записей"] == 2
    assert summary["задумано"] == 70
    assert summary["оборван"] == "квота (429) на записи 3"


def test_full_run_says_so() -> None:
    summary = run.report(
        [outcome(1)], model=FakeCleaner("модель"), wait_sec=30, planned=1, interrupted=None
    )

    assert summary["оборван"] is None


def test_p95_on_few_points_is_marked_as_the_maximum() -> None:
    """При n ≤ 19 ранг без интерполяции даёт максимум — и это надо сказать вслух."""
    few = run.report(
        [outcome(number) for number in range(10)],
        model=FakeCleaner("модель"),
        wait_sec=30,
        planned=10,
        interrupted=None,
    )
    many = run.report(
        [outcome(number) for number in range(25)],
        model=FakeCleaner("модель"),
        wait_sec=30,
        planned=25,
        interrupted=None,
    )

    assert few["p95 достоверен"] is False
    assert many["p95 достоверен"] is True


def test_latency_counts_calls_the_owner_waited_for() -> None:
    """Пустой ответ модели — тоже ожидание: деградация случается **после** ответа.

    Из счёта выпадают только упавшие вызовы: там ответа не было вовсе.
    """
    summary = run.report(
        [
            outcome(1, latency_ms=1000),
            outcome(2, latency_ms=3000, clean=None),
            run.Outcome(
                entry_id=3,
                verbatim="ну надо",
                clean=None,
                report=None,
                latency_ms=50,
                failure="LlmError: 500",
            ),
        ],
        model=FakeCleaner("модель"),
        wait_sec=30,
        planned=3,
        interrupted=None,
    )

    latency = summary["латентность мс"]
    assert isinstance(latency, dict)
    assert latency["максимум"] == 3000
    assert latency["медиана"] == 2000


def test_typo_does_not_burn_the_run(monkeypatch: Any) -> None:
    """Второй уровень идёт после всех вызовов — к этой минуте квота потрачена.

    Опечатка и число не из диапазона переспрашиваются, а не валят прогон
    с непойманным `ValueError`.
    """
    answers = iter(["пять", "99", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert run.ask_number("?", default=3, top=3) == 2


def test_empty_answer_means_everything_survived(monkeypatch: Any) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")

    assert run.ask_number("?", default=3, top=3) == 3


def test_interrupted_review_keeps_what_was_answered(monkeypatch: Any) -> None:
    """Ctrl-C и закрытое окно — это «хватит», а не ошибка."""

    def interrupt(_: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)

    assert run.ask_number("?", default=3, top=3) is None


def test_percentile_is_nearest_rank() -> None:
    assert run.percentile([1, 2, 3, 4], 0.95) == 4
    assert run.percentile(list(range(1, 21)), 0.95) == 19
    assert run.percentile([], 0.95) == 0
