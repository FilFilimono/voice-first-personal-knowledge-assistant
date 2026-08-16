"""Расшифровка голоса.

Здесь стоит проверка инварианта 5: приватная запись наружу не уходит.
Проверка живёт именно тут, а не в вызывающем коде, потому что вызывающий код
не обязан помнить о приватности и не должен иметь возможности её обойти.
Отсюда же следует ограничение приватного режима из ADR-0002: расшифровка
внешняя, поэтому в приватном режиме голосовые не принимаются вовсе.

Провайдер прячется за протоколом `Transcriber` (ADR-0007): смена провайдера —
второй файл рядом, а не правка вызывающего кода.
"""

from brain.config import Settings
from brain.core.ports import Transcriber, Transcription
from brain.core.stt.errors import AudioTooLongError, PrivateContentError, TranscriptionError
from brain.core.stt.gemini import GeminiTranscriber


def build_transcriber(settings: Settings) -> Transcriber:
    """Провайдер по настройке. Пока он один — выбор появится после eval'а."""
    match settings.stt_provider:
        case "gemini":
            return GeminiTranscriber(
                api_key=settings.google_api_key,
                model=settings.stt_model,
            )


async def transcribe_entry(
    transcriber: Transcriber,
    *,
    audio: bytes,
    mime_type: str,
    is_private: bool,
    duration_sec: int | None,
    max_duration_sec: int,
) -> Transcription:
    """Расшифровать голосовое. Единственная дверь наружу для аудио."""
    if is_private:
        raise PrivateContentError(
            "приватная запись не уходит во внешний сервис (инвариант 5): "
            "в приватном режиме голосовые не принимаются вовсе"
        )

    if duration_sec is not None and duration_sec > max_duration_sec:
        raise AudioTooLongError(f"голосовое длиннее {max_duration_sec} секунд: {duration_sec}")

    return await transcriber.transcribe(audio, mime_type)


__all__ = [
    "AudioTooLongError",
    "PrivateContentError",
    "Transcriber",
    "Transcription",
    "TranscriptionError",
    "build_transcriber",
    "transcribe_entry",
]
