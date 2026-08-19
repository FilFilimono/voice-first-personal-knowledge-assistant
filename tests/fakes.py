"""Заглушки портов ядра.

Быстрые тесты обязаны работать без Postgres и без сети: этот же набор гоняет
Stop-хук после каждой сессии. Каждая заглушка не только отвечает, но и помнит,
о чём её спросили — иначе проверять было бы нечего.
"""

import hashlib
import math
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from brain.core.ports import Completion, EntryDraft, ReminderDraft, Transcription
from brain.core.stt.errors import TranscriptionError
from brain.storage.models.chunk import DIMENSIONS


class FakeUnitOfWork:
    """Транзакция приёма в памяти. Уникальность `dedup_key` — как в базе."""

    def __init__(self) -> None:
        self.entries: list[EntryDraft] = []
        self.reminders: list[ReminderDraft] = []
        self.transcripts: list[tuple[int, Transcription]] = []
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

    async def add_transcript(self, entry_id: int, transcription: Transcription) -> None:
        self.transcripts.append((entry_id, transcription))

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
        # Заданные вопросы с кнопками: чат, сообщение, текст, запись.
        self.asked: list[tuple[int, int, str, int]] = []
        # Показанная разметка — то же самое, но с кнопками «не туда»
        # и «сделать приватной».
        self.annotated: list[tuple[int, int, str, int]] = []

    async def send(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edited.append((chat_id, message_id, text))

    async def ask_yes_no(self, chat_id: int, message_id: int, text: str, *, entry_id: int) -> None:
        self.asked.append((chat_id, message_id, text, entry_id))

    async def show_annotation(
        self, chat_id: int, message_id: int, text: str, *, entry_id: int
    ) -> None:
        self.annotated.append((chat_id, message_id, text, entry_id))


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


class StubEmbedder:
    """Модель эмбеддингов, которая никуда не ходит и считает обращения.

    Вектор детерминированный и зависит от текста: два разных куска обязаны
    получить разные вектора, иначе тесты поиска ничего не проверяют.
    Длина единичная — как требует порт `Embedder` от любой реализации.
    """

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self._dimensions = dimensions
        self.documents: list[list[str]] = []
        self.queries: list[str] = []
        self.checked = 0

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-embedding-1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def calls(self) -> int:
        """Сколько раз ходили наружу. Для приватных записей обязан быть ноль."""
        return len(self.documents) + len(self.queries)

    async def check_available(self) -> None:
        self.checked += 1

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self.documents.append(list(texts))
        return [self.vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.vector(text)

    def vector(self, text: str) -> list[float]:
        """Единичный вектор, однозначно определённый текстом."""
        digest = hashlib.sha256(text.encode()).digest()
        raw = [digest[index % len(digest)] - 127.5 for index in range(self._dimensions)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


class RecordedEmbedder:
    """Настоящие вектора, посчитанные один раз настоящей моделью.

    `StubEmbedder` проверяет SQL, но не поиск: у хэша от текста нет смысла,
    и «находится по смыслу, когда общих слов нет» на нём проверить нельзя
    в принципе.

    Данные приходят снаружи — из `tests/archive.py`, который берёт тексты
    из живой базы, а вектора из кэша рядом. В репозитории ни того, ни другого
    нет: архив в git не уходит (ADR-0002).

    Неизвестный текст — ошибка, а не тихий нулевой вектор: молча вернув
    что-нибудь, заглушка превратила бы отсутствие данных в зелёный тест.
    """

    def __init__(self, vectors: dict[str, dict[str, list[float]]], model: str) -> None:
        self._documents = vectors["документы"]
        self._queries = vectors["запросы"]
        self._model = model

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return DIMENSIONS

    async def check_available(self) -> None:
        return None

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._lookup(self._documents, text, "документ") for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._lookup(self._queries, text, "запрос")

    @staticmethod
    def _lookup(source: dict[str, list[float]], text: str, kind: str) -> list[float]:
        try:
            return source[text]
        except KeyError:
            raise AssertionError(
                f"вектор не посчитан ({kind}): {text[:60]!r}. "
                "Добавьте текст в список запросов теста — он досчитается сам."
            ) from None


class ScriptedModel:
    """Модель, отвечающая по сценарию и считающая вызовы.

    Считать вызовы здесь важнее, чем отвечать: два главных утверждения про
    разметку — «пачка уходит одним вызовом на прогон» и «приватная запись
    наружу не уходит» — проверяются именно счётчиком, а не содержимым ответа.

    Сценарий — список ответов на прогоны. Кончился — повторяется последний:
    два прогона одной пачки должны отвечать по-разному только там, где тест
    проверяет расхождение.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.checked = 0

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-flash-lite"

    @property
    def tier(self) -> str:
        return "cheap"

    @property
    def calls(self) -> int:
        return len(self.prompts)

    async def check_available(self) -> None:
        self.checked += 1

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        temperature: float,
    ) -> Completion:
        self.systems.append(system)
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self._script) - 1)
        return Completion(
            data=self._script[index],
            input_tokens=10,
            output_tokens=10,
            latency_ms=1,
        )


def answer(*items: tuple[int, str, list[str]]) -> dict[str, Any]:
    """Ответ прогона: тройки «запись, тип, темы»."""
    return {
        "items": [
            {"id": entry_id, "type": type_, "topics": topics, "entities": []}
            for entry_id, type_, topics in items
        ]
    }


class FakeAudioSource:
    """Файл голосового без Telegram."""

    def __init__(self, audio: bytes = b"OggS-fake") -> None:
        self._audio = audio
        self.downloads: list[str] = []

    async def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self._audio
