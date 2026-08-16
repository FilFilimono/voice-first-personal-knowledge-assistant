"""Инвариант 5: приватная запись не уходит наружу.

Гарантию здесь даёт код, а не математика (ADR-0002), поэтому проверка обязана
жить в быстрых тестах и гоняться на каждом прогоне.

Что должно работать:

1. приватная запись до провайдера не доходит — вызова не происходит вовсе
2. проверка стоит в `core/stt`, а не в вызывающем коде: обойти её,
   позвав функцию иначе, нельзя
3. слишком длинное аудио отклоняется до отправки — деньги не тратятся
4. обычная запись проходит и приносит провенанс: чем и какой моделью
"""

import pytest

from brain.core import stt
from brain.core.ports import Transcription
from brain.core.stt.errors import AudioTooLongError, PrivateContentError
from tests.fakes import StubTranscriber

MAX_DURATION = 900


async def _transcribe(
    transcriber: StubTranscriber,
    *,
    is_private: bool = False,
    duration_sec: int | None = 10,
) -> Transcription:
    return await stt.transcribe_entry(
        transcriber,
        audio=b"OggS",
        mime_type="audio/ogg",
        is_private=is_private,
        duration_sec=duration_sec,
        max_duration_sec=MAX_DURATION,
    )


async def test_private_entry_never_reaches_the_provider() -> None:
    transcriber = StubTranscriber()

    with pytest.raises(PrivateContentError):
        await _transcribe(transcriber, is_private=True)

    assert transcriber.calls == []


async def test_audio_longer_than_the_ceiling_is_refused_before_sending() -> None:
    transcriber = StubTranscriber()

    with pytest.raises(AudioTooLongError):
        await _transcribe(transcriber, duration_sec=MAX_DURATION + 1)

    assert transcriber.calls == []


async def test_privacy_is_checked_before_the_length() -> None:
    """Приватность важнее: длинную приватную запись тоже нельзя отправлять."""
    transcriber = StubTranscriber()

    with pytest.raises(PrivateContentError):
        await _transcribe(transcriber, is_private=True, duration_sec=MAX_DURATION + 1)


async def test_ordinary_entry_is_transcribed_with_provenance() -> None:
    transcriber = StubTranscriber("надо переписать ретривал")

    result = await _transcribe(transcriber)

    assert transcriber.calls == [(b"OggS", "audio/ogg")]
    assert result.text == "надо переписать ретривал"
    assert result.provider == "stub"
    assert result.model == "stub-1"


async def test_unknown_duration_is_allowed() -> None:
    """Telegram всегда присылает длительность, но её отсутствие — не повод падать."""
    transcriber = StubTranscriber()

    await _transcribe(transcriber, duration_sec=None)

    assert len(transcriber.calls) == 1
