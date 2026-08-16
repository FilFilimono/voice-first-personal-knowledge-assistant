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
from typing import Any

import httpx

from brain.core.ports import Transcription
from brain.core.stt.errors import TranscriptionError

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

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

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
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                response = await client.post(
                    ENDPOINT.format(model=self._model),
                    # Ключ заголовком, а не в URL: адреса попадают в логи и метрики.
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise TranscriptionError(f"провайдер не ответил: {error}") from error

        latency_ms = int((time.monotonic() - started) * 1000)
        return Transcription(
            text=_extract_text(payload),
            provider=self.provider,
            model=self._model,
            latency_ms=latency_ms,
        )


def _extract_text(payload: dict[str, Any]) -> str:
    """Достать текст из ответа, не притворяясь, что пустой ответ — это успех."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts).strip()
    except (KeyError, IndexError, TypeError) as error:
        raise TranscriptionError(f"ответ провайдера не разобран: {payload}") from error

    if not text:
        # Пустая расшифровка — не «тишина в записи», а повод повторить:
        # молча сохранив пустоту, мы потеряем сказанное безвозвратно.
        raise TranscriptionError("провайдер вернул пустую расшифровку")
    return text
