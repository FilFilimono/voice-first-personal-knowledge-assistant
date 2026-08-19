"""Расшифровка аудио напрямую мультимодальной моделью Gemini.

Ветка «аудио в мультимодальную модель» из ADR-0007, выбранная по умолчанию
в ADR-0009: ключ Google уже нужен проекту для эмбеддингов, поэтому третий
счёт не заводится до того, как eval покажет, что он нужен.

SDK провайдера намеренно не берётся: запрос — один POST, а SDK привязал бы
модуль к вендору сильнее, чем предполагает интерфейс. Весь протокол виден
в этом файле целиком, и второй провайдер — такой же файл рядом.

OGG/Opus от Telegram уходит как есть: Gemini принимает его сам, ffmpeg
в образе не нужен.
"""

import base64
import time
from collections.abc import Callable
from typing import Any

import httpx

from brain.core.ports import Transcription
from brain.core.stt.errors import ProviderUnavailableError, TranscriptionError

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Промпт делает ровно одну вещь: просит текст и ничего больше. Модель охотно
# добавляет «вот расшифровка:» и пересказ, а нам нужна дословная речь —
# всё остальное портит и поиск, и будущую разметку.
PROMPT = (
    "Расшифруй эту аудиозапись дословно. "
    "Сохрани язык оригинала, включая английские термины внутри русской речи. "
    "Не переводи, не пересказывай, не исправляй и не комментируй. "
    "Верни только текст записи, без вступлений и пояснений."
)

# Провайдер может отвечать долго на длинном аудио. Ретраями занимается очередь,
# поэтому здесь достаточно потолка, за которым ожидание перестаёт быть осмысленным.
TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)


class GeminiTranscriber:
    """Один POST с аудио внутри запроса."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # Шов для тестов: разбор ответов провайдера — самая ошибкоопасная часть
        # этого файла, и проверять её надо на настоящем коде, но без сети.
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT)

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    async def check_available(self) -> None:
        """Дёрнуть модель самым дешёвым запросом, какой есть.

        Списку моделей верить нельзя: `GET /v1beta/models` показывает модель
        и заявляет `generateContent`, а сам вызов отвечает 404 «снята
        с обслуживания для новых пользователей» — ровно это случилось
        с `gemini-2.5-flash-lite`. Единственная надёжная проверка — вызов.

        Стоит один входной токен и происходит один раз за старт воркера.
        """
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": "ok"}]}],
            # Ответ не нужен, нужен факт того, что запрос принят.
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1},
        }

        try:
            async with self._client_factory() as client:
                response = await client.post(
                    ENDPOINT.format(model=self._model),
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(
                f"провайдер {self.provider} недоступен (модель {self._model}): {error}"
            ) from error

        if response.is_success:
            return

        raise ProviderUnavailableError(
            f"модель {self._model} не отвечает на generateContent: "
            f"HTTP {response.status_code}. {_error_message(response)}",
            status=response.status_code,
        )

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        body: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(audio).decode(),
                            }
                        },
                    ]
                }
            ],
            # Расшифровка — не творческая задача: разброс здесь только вредит.
            "generationConfig": {"temperature": 0.0},
        }

        started = time.monotonic()
        async with self._client_factory() as client:
            try:
                response = await client.post(
                    ENDPOINT.format(model=self._model),
                    # Ключ заголовком, а не в URL: адреса попадают в логи и метрики.
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as error:
                # Имя модели — в тексте ошибки. Без него «HTTP 404» в логе
                # не говорит ничего: настройка живёт в `.env`, а не в коде,
                # и первый вопрос всегда «а какая модель-то была».
                # Код едет объектом, а не только в тексте: по тексту старт
                # не отличал кончившуюся квоту от снятой модели.
                raise TranscriptionError(
                    f"{self.provider}/{self._model}: HTTP {error.response.status_code}. "
                    f"{_error_message(error.response)}",
                    status=error.response.status_code,
                ) from error
            except (httpx.HTTPError, ValueError) as error:
                raise TranscriptionError(
                    f"{self.provider}/{self._model} не ответил: {error}"
                ) from error

        latency_ms = int((time.monotonic() - started) * 1000)
        return Transcription(
            text=self._extract_text(payload),
            provider=self.provider,
            model=self._model,
            latency_ms=latency_ms,
        )

    def _extract_text(self, payload: dict[str, Any]) -> str:
        """Достать текст из ответа, не притворяясь, что пустой ответ — это успех."""
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts).strip()
        except (KeyError, IndexError, TypeError) as error:
            raise TranscriptionError(
                f"{self.provider}/{self._model}: ответ не разобран: {payload}"
            ) from error

        if not text:
            # Пустая расшифровка — не «тишина в записи», а повод повторить:
            # молча сохранив пустоту, мы потеряем сказанное безвозвратно.
            raise TranscriptionError(f"{self.provider}/{self._model} вернул пустую расшифровку")
        return text


def _error_message(response: httpx.Response) -> str:
    """Объяснение провайдера, если оно есть.

    Google кладёт в тело осмысленный текст — в нашем случае это была фраза
    «no longer available to new users», то есть готовый ответ на вопрос
    «что делать». Терять её ради аккуратного лога не стоит.
    """
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]
    return str(message)
