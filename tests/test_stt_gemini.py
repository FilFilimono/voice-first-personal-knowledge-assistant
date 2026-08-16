"""Что должно работать (написано по реальному инциденту 16 августа 2026):

`gemini-2.5-flash-lite` присутствовала в `GET /v1beta/models` и заявляла
поддержку `generateContent`, но каждый вызов отвечал 404 «no longer available
to new users». Проверка по списку моделей этот случай пропускает — поэтому:

1. проверка доступности — настоящий вызов, а не сверка со списком
2. недоступная модель валит старт воркера с ошибкой, в которой видно **имя**
3. имя модели есть в любой ошибке расшифровки: настройка живёт в `.env`,
   и первый вопрос по логу всегда «а какая модель-то была»
4. объяснение провайдера доходит до нас, а не теряется ради аккуратного текста
5. пустая расшифровка — ошибка, а не «в записи была тишина»
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from brain.core.stt.errors import ProviderUnavailableError, TranscriptionError
from brain.core.stt.gemini import GeminiTranscriber

MODEL = "gemini-3.5-flash-lite"
RETIRED = "gemini-2.5-flash-lite"

Handler = Callable[[httpx.Request], httpx.Response]

# Тело, которым Google ответил на снятую с обслуживания модель.
GONE: dict[str, Any] = {
    "error": {
        "code": 404,
        "message": (
            f"This model models/{RETIRED} is no longer available to new users. "
            "Please update your code to use a newer model."
        ),
        "status": "NOT_FOUND",
    }
}

SPOKEN: dict[str, Any] = {
    "candidates": [{"content": {"parts": [{"text": " надо переписать ретривал "}]}}]
}


def _transcriber(model: str, handler: Handler) -> GeminiTranscriber:
    """Провайдер с подменённым транспортом: настоящий разбор, но без сети."""
    return GeminiTranscriber(
        api_key="test-key",
        model=model,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _responds(status: int, payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


async def test_retired_model_fails_the_availability_check() -> None:
    transcriber = _transcriber(RETIRED, _responds(404, GONE))

    with pytest.raises(ProviderUnavailableError) as error:
        await transcriber.check_available()

    message = str(error.value)
    assert RETIRED in message
    # Объяснение провайдера — готовый ответ на вопрос «что делать».
    assert "no longer available" in message


async def test_working_model_passes_the_availability_check() -> None:
    transcriber = _transcriber(MODEL, _responds(200, {"candidates": []}))

    await transcriber.check_available()


async def test_availability_check_is_a_real_call_not_a_model_listing() -> None:
    """Списку моделей верить нельзя: он показывал снятую модель как рабочую."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"candidates": []})

    await _transcriber(MODEL, handler).check_available()

    assert requested == [f"POST /v1beta/models/{MODEL}:generateContent"]


async def test_model_name_is_in_the_transcription_error() -> None:
    transcriber = _transcriber(RETIRED, _responds(404, GONE))

    with pytest.raises(TranscriptionError) as error:
        await transcriber.transcribe(b"OggS", "audio/ogg")

    message = str(error.value)
    assert RETIRED in message
    assert "404" in message
    assert "no longer available" in message


async def test_empty_transcription_is_an_error_with_the_model_name() -> None:
    empty = {"candidates": [{"content": {"parts": [{"text": "   "}]}}]}
    transcriber = _transcriber(MODEL, _responds(200, empty))

    with pytest.raises(TranscriptionError) as error:
        await transcriber.transcribe(b"OggS", "audio/ogg")

    assert MODEL in str(error.value)


async def test_transcription_returns_text_and_provenance() -> None:
    transcriber = _transcriber(MODEL, _responds(200, SPOKEN))

    result = await transcriber.transcribe(b"OggS", "audio/ogg")

    assert result.text == "надо переписать ретривал"
    assert (result.provider, result.model) == ("gemini", MODEL)


async def test_audio_goes_as_is_without_reencoding() -> None:
    """OGG/Opus от Telegram Gemini принимает сам — ffmpeg в образе не нужен."""
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=SPOKEN)

    await _transcriber(MODEL, handler).transcribe(b"OggS-audio", "audio/ogg")

    parts = seen[0]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "audio/ogg"
