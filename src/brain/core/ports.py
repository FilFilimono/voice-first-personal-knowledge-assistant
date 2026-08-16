"""Границы ядра: что ядру нужно снаружи и в каком виде.

Здесь нет ни SQLAlchemy, ни aiogram, ни httpx — только протоколы и маленькие
неизменяемые значения, которыми ядро обменивается с окружением. Из-за этого
работают две вещи сразу:

- инвариант 1 (адаптеры не ходят в БД) держится конструкцией: адаптер физически
  не может ничего записать, кроме как через переданный ему объект ядра;
- быстрые тесты обходятся без Postgres и без сети — на месте реализаций стоят
  фейки на два десятка строк. Stop-хук гоняет их после каждой сессии, и он
  обязан отрабатывать за секунды.

Протоколы описывают то, что ядру нужно, а не то, что умеет реализация: список
методов здесь короткий намеренно.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from brain.storage.models.entry import EntryKind, EntrySource


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """Запись, готовая к вставке. После вставки не меняется никогда."""

    source: EntrySource
    kind: EntryKind
    is_private: bool
    timezone: str
    # Защита от двойной доставки. Для Telegram — идентификатор сообщения.
    dedup_key: str

    raw_text: str | None = None
    audio_path: str | None = None
    duration_sec: int | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReminderDraft:
    """Напоминание, разобранное из текста записи."""

    source_entry_id: int
    fire_at: datetime
    text: str


class CaptureUnitOfWork(Protocol):
    """Одна транзакция приёма.

    Инвариант 7 держится здесь: всё, что вызвано на одном объекте, попадает
    в базу вместе или не попадает вовсе. Поэтому постановка задания — метод
    той же единицы работы, а не отдельный сервис: разнести их значит вернуть
    подчищающий sweep, от которого избавился ADR-0005.
    """

    async def add_entry(self, draft: EntryDraft) -> int | None:
        """Вставить запись. `None`, если запись с таким `dedup_key` уже есть."""
        ...

    async def add_reminder(self, draft: ReminderDraft) -> int:
        """Вставить напоминание."""
        ...

    async def enqueue(
        self,
        entrypoint: str,
        payload: Mapping[str, Any],
        *,
        execute_after: timedelta | None = None,
    ) -> None:
        """Поставить задание в очередь — в этой же транзакции."""
        ...


class Notifier(Protocol):
    """Канал, которым система разговаривает с владельцем.

    Ядро не знает, что это Telegram: воркеру нужно уметь отправить сообщение
    и отредактировать раннее отправленное — этим список и исчерпывается.
    """

    async def send(self, chat_id: int, text: str) -> int:
        """Отправить сообщение, вернуть его идентификатор."""
        ...

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Заменить текст ранее отправленного сообщения.

        Одна запись стоит одного уведомления: подтверждение приёма и готовый
        разбор — это одно и то же сообщение в разные моменты времени.
        """
        ...


class AudioSource(Protocol):
    """Откуда берётся файл голосового. У Telegram — по идентификатору файла."""

    async def download(self, file_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Transcription:
    """Результат расшифровки вместе с провенансом: чем именно услышано."""

    text: str
    provider: str
    model: str
    latency_ms: int


class Transcriber(Protocol):
    """Провайдер расшифровки.

    Инвариант 2: прямые вызовы SDK провайдера живут только за этой границей.
    Смена провайдера — второй файл в `core/stt`, а не правка вызывающего кода
    (ADR-0007).
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription: ...

    async def check_available(self) -> None:
        """Убедиться, что моделью действительно можно пользоваться.

        Зовётся при старте воркера. Проверка обязана быть **настоящим вызовом**,
        а не сверкой со списком моделей: список у провайдера может показывать
        модель и заявлять поддержку метода, а сам вызов при этом отвечать 404
        «снята с обслуживания для новых пользователей». Так и случилось
        с `gemini-2.5-flash-lite` 16 августа 2026.
        """
        ...
