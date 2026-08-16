"""Заглушки портов ядра.

Быстрые тесты обязаны работать без Postgres и без сети: этот же набор гоняет
Stop-хук после каждой сессии. Каждая заглушка не только отвечает, но и помнит,
о чём её спросили — иначе проверять было бы нечего.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from brain.core.ports import EntryDraft, ReminderDraft, Transcription
from brain.core.stt.errors import TranscriptionError


class FakeUnitOfWork:
    """Транзакция приёма в памяти. Уникальность `dedup_key` — как в базе."""

    def __init__(self) -> None:
        self.entries: list[EntryDraft] = []
        self.reminders: list[ReminderDraft] = []
        self.jobs: list[tuple[str, Mapping[str, Any], timedelta | None]] = []
        self._keys: set[str] = set()

    async def add_entry(self, draft: EntryDraft) -> int | None:
        if draft.dedup_key in self._keys:
            return None
        self._keys.add(draft.dedup_key)
        self.entries.append(draft)
        return len(self.entries)

    async def add_reminder(self, draft: ReminderDraft) -> int:
        self.reminders.append(draft)
        return len(self.reminders)

    async def enqueue(
        self,
        entrypoint: str,
        payload: Mapping[str, Any],
        *,
        execute_after: timedelta | None = None,
    ) -> None:
        self.jobs.append((entrypoint, payload, execute_after))

    def jobs_named(self, entrypoint: str) -> list[tuple[str, Mapping[str, Any], timedelta | None]]:
        return [job for job in self.jobs if job[0] == entrypoint]


class FakeNotifier:
    """Канал к владельцу, который никуда не ходит."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []

    async def send(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))


class StubTranscriber:
    """Провайдер, который всегда слышит одно и то же и считает вызовы."""

    def __init__(self, text: str = "расшифровано") -> None:
        self._text = text
        self.calls: list[tuple[bytes, str]] = []
        self.checked = 0

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-1"

    async def check_available(self) -> None:
        self.checked += 1

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        self.calls.append((audio, mime_type))
        return Transcription(
            text=self._text, provider=self.provider, model=self.model, latency_ms=1
        )


class FailingTranscriber:
    """Провайдер, который лежит. Считает попытки — по ним видно ретраи."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider(self) -> str:
        return "failing"

    @property
    def model(self) -> str:
        return "failing-1"

    async def check_available(self) -> None:
        return None

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        self.calls += 1
        raise TranscriptionError("провайдер не ответил")


class FakeAudioSource:
    """Файл голосового без Telegram."""

    def __init__(self, audio: bytes = b"OggS-fake") -> None:
        self._audio = audio
        self.downloads: list[str] = []

    async def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self._audio
