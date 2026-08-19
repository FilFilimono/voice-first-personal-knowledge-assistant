"""Старт воркера: из-за чего он обязан упасть, а из-за чего — нет.

Быстрые тесты, без сети и базы: проверяются модели-заглушки, которые отвечают
заданным кодом.

**Что здесь на самом деле проверяется.** 20 августа 2026 кончилась бесплатная
суточная квота эмбеддингов, провайдер ответил `429`, и воркер не поднялся
вовсе. Вместе с эмбеддингами встали расшифровка, напоминания и утренняя
сводка — то есть в том числе «заказанное», о чём система не имеет права
молчать (`vision.md` §7). Квота при этом проходит сама к полуночи.

Обратная сторона проверяется тем же файлом: неверное имя модели по-прежнему
обязано ронять старт. Иначе вернётся поломка 16 августа — система выглядит
работающей, а расшифровок нет, и выясняется это через неделю.
"""

from collections.abc import Awaitable, Callable

import pytest

from brain.core.stt.errors import ProviderUnavailableError as SttUnavailable
from brain.worker import check_models

Check = Callable[[], Awaitable[None]]


async def _ok() -> None:
    return None


def _failing(status: int | None, message: str = "провайдер отказал") -> Check:
    async def check() -> None:
        raise SttUnavailable(message, status=status)

    return check


async def test_quota_lets_the_worker_start() -> None:
    """429 — предупреждение, а не остановка. Это и есть починка."""
    warnings = await check_models([("эмбеддингов gemini/x", "EMBEDDING_MODEL", _failing(429))])

    assert len(warnings) == 1
    assert "429" in warnings[0] or "квота" in warnings[0].lower()


async def test_provider_outage_lets_the_worker_start() -> None:
    """503 — сбой на стороне провайдера, проходит сам."""
    warnings = await check_models([("разметки gemini/x", "ENRICH_MODEL", _failing(503))])

    assert len(warnings) == 1


async def test_retired_model_still_stops_the_worker() -> None:
    """404 сам не пройдёт: пока не поправят имя, ответ будет тот же.

    Ослабить это нельзя — молчаливый старт с неверной моделью и есть дыра
    16 августа 2026.
    """
    with pytest.raises(SystemExit) as stopped:
        await check_models([("расшифровки gemini/x", "STT_MODEL", _failing(404))])

    assert "STT_MODEL" in str(stopped.value)


async def test_quota_advice_does_not_send_the_owner_to_env() -> None:
    """Старый текст советовал «поправьте EMBEDDING_MODEL» — для квоты это ложь."""
    warnings = await check_models([("эмбеддингов gemini/x", "EMBEDDING_MODEL", _failing(429))])

    assert "EMBEDDING_MODEL" not in warnings[0]
    assert "проект" in warnings[0]


async def test_region_refusal_names_the_region_not_the_model() -> None:
    """400 «User location is not supported» уже клал разметку 19 августа.

    По тексту «HTTP 400» понять, что дело в регионе, было нельзя, а совет
    предлагал править имя модели.
    """
    with pytest.raises(SystemExit) as stopped:
        await check_models(
            [
                (
                    "разметки gemini/x",
                    "ENRICH_MODEL",
                    _failing(400, "User location is not supported for the API use."),
                )
            ]
        )

    assert "регион" in str(stopped.value).lower()
    assert "ENRICH_MODEL" not in str(stopped.value)


async def test_all_models_are_checked_even_after_a_warning() -> None:
    """Временный отказ одной модели не отменяет проверку остальных.

    Иначе неверное имя второй модели выяснилось бы только назавтра, когда
    пройдёт квота у первой.
    """
    with pytest.raises(SystemExit):
        await check_models(
            [
                ("эмбеддингов gemini/x", "EMBEDDING_MODEL", _failing(429)),
                ("разметки gemini/y", "ENRICH_MODEL", _failing(404)),
            ]
        )


async def test_healthy_models_produce_no_warnings() -> None:
    assert await check_models([("расшифровки gemini/x", "STT_MODEL", _ok)]) == []
