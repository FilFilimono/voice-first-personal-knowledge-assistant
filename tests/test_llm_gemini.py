"""Что должно работать:

1. уровень рассуждения уходит **вложенным** — `generationConfig.thinkingConfig.thinkingLevel`
2. уровень не задан — тело запроса ровно то же, что было до появления настройки
3. температура остаётся на месте, когда уровень задан
4. `build_model` доносит уровень из настройки до самого запроса
5. проба при старте шлёт **настоящую схему** — объект со строковым полем
6. отказ на пробе доносит код ответа: по нему старт решает, вставать или нет

**Про пункт 1.** Проверено живым вызовом 25 августа 2026: плоское
`generationConfig.thinkingLevel` провайдер отвергает с
`400 Unknown name "thinkingLevel" at 'generation_config': Cannot find field`.
Вложенность здесь — не стиль, а протокол.

**Про пункт 2.** Уровень нужен одной модели из трёх, а тем же классом ходят
разметка (`gemini-3.5-flash-lite`), у которой ненулевая температура работает
измерительным прибором (ADR-0011). Лишний ключ в теле её запроса менял бы
поведение модели, о котором сняты замеры, — поэтому «не попросили» обязано
означать «ничего не изменилось», и это утверждение под тестом целиком,
сравнением всего тела, а не отсутствием одного ключа.

**Про пункт 5.** Дыра, найденная 25 августа 2026: проба слала `"ok"` и схему
из одного булева поля, то есть ловила «модели нет» и не ловила «модель есть
и бесполезна». Воркер написал в лог «модель чистки доступна» про модель,
которая на рабочем запросе не отвечает. Длина текста при этом минимальная
намеренно: проверяется, что модель умеет ответить по схеме, а не то,
как быстро она это делает.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from brain.config import Settings
from brain.core import llm
from brain.core.llm.gemini import GeminiChat

MODEL = "gemini-3.5-flash-lite"

Handler = Callable[[httpx.Request], httpx.Response]

# Ответ провайдера в том виде, в каком он приходит на схему со строковым полем.
ANSWER: dict[str, Any] = {
    "candidates": [
        {"content": {"parts": [{"text": '{"text": "готово"}'}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
}

# Модели нет. Google отвечает так же, как ответил на снятую с обслуживания
# `gemini-2.5-flash-lite` 16 августа 2026.
GONE: dict[str, Any] = {
    "error": {"code": 404, "message": "models/нет-такой is not found", "status": "NOT_FOUND"}
}

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}}


def _recording(seen: list[dict[str, Any]]) -> Handler:
    """Отвечает как провайдер и запоминает, о чём спросили."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=ANSWER)

    return handler


def _responds(status: int, payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _transport(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat(
    handler: Handler,
    *,
    model: str = MODEL,
    thinking_level: str | None = None,
) -> GeminiChat:
    return GeminiChat(
        api_key="test-key",
        model=model,
        thinking_level=thinking_level,
        client_factory=_transport(handler),
    )


# --- Уровень рассуждения ---


async def test_thinking_level_goes_nested_under_thinking_config() -> None:
    """Плоское `generationConfig.thinkingLevel` провайдер отвергает 400-й."""
    seen: list[dict[str, Any]] = []

    await _chat(_recording(seen), thinking_level="low").complete(
        system="инструкция", prompt="текст", schema=SCHEMA, temperature=0.0
    )

    config = seen[0]["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "thinkingLevel" not in config, "плоский ключ провайдер не принимает"


async def test_body_without_a_thinking_level_is_exactly_what_it_was() -> None:
    """Не попросили уровень — тело обязано остаться прежним целиком.

    Сравнение всего тела, а не отсутствия одного ключа: этим же классом ходят
    разметка, у которой температура — измерительный прибор (ADR-0011).
    """
    seen: list[dict[str, Any]] = []

    await _chat(_recording(seen)).complete(
        system="инструкция", prompt="текст", schema=SCHEMA, temperature=0.7
    )

    assert seen[0] == {
        "systemInstruction": {"parts": [{"text": "инструкция"}]},
        "contents": [{"parts": [{"text": "текст"}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }


async def test_temperature_survives_the_thinking_level() -> None:
    """Температуру снимать нельзя: без разброса нет расхождения двух прогонов."""
    seen: list[dict[str, Any]] = []

    await _chat(_recording(seen), thinking_level="high").complete(
        system="инструкция", prompt="текст", schema=SCHEMA, temperature=1.0
    )

    assert seen[0]["generationConfig"]["temperature"] == 1.0


async def test_build_model_carries_the_thinking_level_to_the_request() -> None:
    """Уровень — свойство модели, и настройка обязана доехать до тела запроса."""
    seen: list[dict[str, Any]] = []
    model = llm.build_model(
        Settings(google_api_key="test-key"),
        model=MODEL,
        thinking_level="low",
    )
    assert isinstance(model, GeminiChat)
    # Шов подменяется после сборки: `build_model` про сеть ничего не знает
    # намеренно, а проверять надо именно то, что уехало провайдеру.
    model._client_factory = _transport(_recording(seen))

    await model.complete(system="инструкция", prompt="текст", schema=SCHEMA, temperature=0.0)

    assert seen[0]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"


# --- Проба при старте ---


async def test_the_startup_probe_asks_for_a_real_schema() -> None:
    """Схема из одного булева поля ловила «модели нет» и не ловила бесполезную.

    25 августа 2026 воркер написал «модель чистки доступна» про модель,
    которая на рабочем запросе не отвечает.
    """
    seen: list[dict[str, Any]] = []

    await llm.check_available(_chat(_recording(seen)))

    schema = seen[0]["generationConfig"]["responseSchema"]
    assert schema["type"] == "object"
    assert [field["type"] for field in schema["properties"].values()] == ["string"], (
        "проба обязана просить строку — ровно то, что просит чистка"
    )


async def test_the_startup_probe_keeps_the_status_code() -> None:
    """По коду старт решает: временный отказ — предупреждение, `404` — остановка."""
    with pytest.raises(llm.ProviderUnavailableError) as refused:
        await llm.check_available(_chat(_responds(404, GONE), model="нет-такой"))

    assert refused.value.status == 404
    assert "нет-такой" in str(refused.value)
