"""Что должно работать:

1. вектор, отданный наружу, единичной длины — даже если провайдер прислал не такой
2. документ и запрос считаются разными `taskType` — модель обучена на асимметрии
3. размерность просится явно и совпадает с той, что в схеме
4. батч сохраняет порядок: чанк 3 обязан получить вектор чанка 3
5. недоступная модель валит проверку, и в ошибке видно её имя
6. пустой или короткий ответ — ошибка, а не «столько и посчиталось»

**Про пункт 1.** Проверено вызовом 18 августа 2026: `gemini-embedding-001`
на срезе MRL до 768 отдаёт вектор с нормой **≈0.58**, до 1536 — **≈0.69**,
и только полные 3072 приходят единичными. `gemini-embedding-2` нормирован
на всех размерностях. Ответ провайдера здесь настоящий, записан целиком
в `tests/data/neutral_embeddings.json` — придуманный вектор проверял бы
представления автора о провайдере, а не провайдера.

Тексты там **нейтральные, не из архива**: нормы вектора от содержания
не зависят, а архив в git не попадает (ADR-0002).

Косинус масштабно-инвариантен, поэтому на ранжирование по `<=>` ненормированный
вектор не влияет вовсе — тест «косинус поехал» покраснеть не может. Ломается
другое, и молча: `<#>` и `<->` дают другой порядок, порог связей 0.75
из ADR-0006 перестаёт значить написанное, а смесь векторов двух норм в одной
таблице портит любое сравнение, кроме косинуса. Отсюда и пункт 1, и отдельный
тест на живой базе в `test_embeddings_normalization.py`.
"""

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from brain.core.retrieval.embeddings import EmbeddingError, ProviderUnavailableError
from brain.core.retrieval.gemini import GeminiEmbedder
from brain.storage.models.chunk import DIMENSIONS

MODEL = "gemini-embedding-001"

Handler = Callable[[httpx.Request], httpx.Response]

# Настоящий ответ провайдера на нейтральном тексте, записанный 19 августа 2026.
RECORDED: dict[str, Any] = json.loads(
    (Path(__file__).parent / "data" / "neutral_embeddings.json").read_text()
)
REAL_VALUES: list[float] = RECORDED["gemini-embedding-001"]["документы"]["погода"]

# Модель, которой нет. Google отвечает так же, как ответил на снятую с
# обслуживания `gemini-2.5-flash-lite` 16 августа.
GONE: dict[str, Any] = {
    "error": {"code": 404, "message": "models/нет-такой is not found", "status": "NOT_FOUND"}
}


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _embedder(handler: Handler, model: str = MODEL) -> GeminiEmbedder:
    return GeminiEmbedder(
        api_key="test-key",
        model=model,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _responds(status: int, payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _recording(seen: list[dict[str, Any]]) -> Handler:
    """Отвечает настоящим вектором и запоминает, о чём спросили."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        count = len(body.get("requests", [1]))
        return httpx.Response(200, json={"embeddings": [{"values": REAL_VALUES}] * count})

    return handler


# --- Нормировка ---


def test_recorded_response_is_really_unnormalised() -> None:
    """Если это когда-нибудь перестанет быть правдой — тесты ниже бессмысленны."""
    assert abs(_norm(REAL_VALUES) - 1.0) > 0.3, (
        "провайдер стал отдавать нормированный срез — проверить и упростить"
    )


async def test_document_vector_is_unit_length() -> None:
    embedder = _embedder(_recording([]))

    [vector] = await embedder.embed_documents(["Сегодня в городе шёл дождь."])

    assert _norm(vector) == pytest.approx(1.0)


async def test_query_vector_is_unit_length() -> None:
    embedder = _embedder(_recording([]))

    vector = await embedder.embed_query("какая была погода")

    assert _norm(vector) == pytest.approx(1.0)


async def test_normalisation_keeps_direction() -> None:
    """Нормировка меняет длину, а не смысл: направление обязано остаться."""
    embedder = _embedder(_recording([]))

    [vector] = await embedder.embed_documents(["Сегодня в городе шёл дождь."])

    scale = _norm(REAL_VALUES)
    assert vector == pytest.approx([x / scale for x in REAL_VALUES])


# --- Что уходит провайдеру ---


async def test_document_and_query_use_different_task_types() -> None:
    """Модель обучена на асимметрии: запрос и документ считаются по-разному."""
    seen: list[dict[str, Any]] = []
    embedder = _embedder(_recording(seen))

    await embedder.embed_documents(["кусок записи"])
    await embedder.embed_query("вопрос")

    assert seen[0]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert seen[1]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


async def test_dimension_is_asked_for_explicitly() -> None:
    """Умолчание провайдера — 3072, а в колонке 768: молчать здесь нельзя."""
    seen: list[dict[str, Any]] = []
    embedder = _embedder(_recording(seen))

    await embedder.embed_documents(["кусок записи"])

    assert seen[0]["requests"][0]["outputDimensionality"] == DIMENSIONS
    assert embedder.dimensions == DIMENSIONS


async def test_batch_goes_in_one_request() -> None:
    """Чанки записи считаются пачкой: это один вызов вместо N."""
    seen: list[dict[str, Any]] = []
    embedder = _embedder(_recording(seen))

    await embedder.embed_documents(["раз", "два", "три"])

    assert len(seen) == 1
    assert len(seen[0]["requests"]) == 3


async def test_batch_keeps_order() -> None:
    """Вектор чанка 3 обязан достаться чанку 3, иначе поиск врёт незаметно."""
    order = ["первый", "второй", "третий"]

    def handler(request: httpx.Request) -> httpx.Response:
        texts = [
            item["content"]["parts"][0]["text"] for item in json.loads(request.content)["requests"]
        ]
        # Вектор-метка: первая координата — номер текста в исходном списке.
        return httpx.Response(
            200,
            json={
                "embeddings": [
                    {"values": [float(order.index(text) + 1)] + [0.0] * (DIMENSIONS - 1)}
                    for text in texts
                ]
            },
        )

    vectors = await _embedder(handler).embed_documents(order)

    assert [vector[0] for vector in vectors] == [1.0, 1.0, 1.0]  # после нормировки — единицы
    assert len(vectors) == 3


async def test_empty_batch_asks_nobody() -> None:
    """Пустой список — не повод ходить в сеть и платить за это."""
    seen: list[dict[str, Any]] = []

    assert await _embedder(_recording(seen)).embed_documents([]) == []
    assert seen == []


# --- Отказы ---


async def test_missing_model_fails_the_check_by_name() -> None:
    embedder = _embedder(_responds(404, GONE), model="нет-такой")

    with pytest.raises(ProviderUnavailableError) as error:
        await embedder.check_available()

    assert "нет-такой" in str(error.value)
    assert "is not found" in str(error.value), "объяснение провайдера потеряно"


async def test_check_passes_on_a_real_answer() -> None:
    await _embedder(_recording([])).check_available()


async def test_short_vector_is_an_error() -> None:
    """Размерность не та — значит, в колонку это не ляжет. Лучше узнать здесь."""
    embedder = _embedder(_responds(200, {"embeddings": [{"values": [0.1, 0.2, 0.3]}]}))

    with pytest.raises(EmbeddingError) as error:
        await embedder.embed_documents(["кусок"])

    assert str(DIMENSIONS) in str(error.value)


async def test_missing_vector_is_an_error() -> None:
    """Ответ есть, вектора нет: молча вернуть ноль векторов нельзя."""
    embedder = _embedder(_responds(200, {"embeddings": []}))

    with pytest.raises(EmbeddingError):
        await embedder.embed_documents(["кусок"])


async def test_zero_vector_is_an_error() -> None:
    """Нулевой вектор не нормируется — и означает, что считать было нечего."""
    embedder = _embedder(_responds(200, {"embeddings": [{"values": [0.0] * DIMENSIONS}]}))

    with pytest.raises(EmbeddingError):
        await embedder.embed_documents(["кусок"])


async def test_model_name_is_in_the_failure() -> None:
    """Настройка живёт в `.env`: первый вопрос по логу — «какая модель была»."""
    embedder = _embedder(_responds(500, {"error": {"message": "внутренняя ошибка"}}))

    with pytest.raises(EmbeddingError) as error:
        await embedder.embed_documents(["кусок"])

    assert MODEL in str(error.value)
