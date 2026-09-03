"""Что происходит с сообщением между «пришло» и «сохранено».

Порядок в приёме голосового важен и выбран не случайно:

1. приватный режим проверяется **до всего остального** — в приватном режиме
   голосовое отклоняется, файл не скачивается, наружу не уходит ничего;
2. запись и задание пишутся одной транзакцией (инвариант 7);
3. в задании лежат только идентификаторы: `entry_id` и `message_id`
   подтверждения, которое воркер потом отредактирует.

Аудио здесь не скачивается намеренно. Скачиванием занимается воркер: между
подтверждением и долговечной записью должно пройти как можно меньше времени,
а сетевая операция ещё и падает — в воркере она попадает под ретраи очереди,
в обработчике не попала бы никуда.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from brain.core.ports import CaptureUnitOfWork, EntryDraft
from brain.core.scheduler import reminders
from brain.core.scheduler.queue import ENRICH, INDEX, TRANSCRIBE
from brain.storage.models.entry import EntryKind, EntrySource

# Расширение сохраняется как есть: Telegram отдаёт OGG/Opus, провайдер
# принимает его напрямую, перекодирование не нужно (ADR-0007).
VOICE_SUFFIX = ".oga"
VOICE_MIME = "audio/ogg"


@dataclass(frozen=True, slots=True)
class IncomingText:
    """Текстовое сообщение, разобранное адаптером."""

    chat_id: int
    message_id: int
    ack_message_id: int
    text: str
    # На что это ответ, если владелец отвечает на вопрос раскачки (`vision.md`
    # §5.9). Обычная запись — это `None`, и таких большинство.
    answers: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class IncomingVoice:
    """Голосовое сообщение, разобранное адаптером."""

    chat_id: int
    message_id: int
    ack_message_id: int
    file_id: str
    duration_sec: int
    answers: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Что показать владельцу и что произошло с записью."""

    reply: str
    entry_id: int | None = None
    # Повторная доставка того же сообщения. Записи не прибавилось,
    # и говорить об этом не надо: владелец отправлял сообщение один раз.
    duplicate: bool = False
    # Запись похожа на напоминание, но слова «напомни» в ней не было.
    # Адаптер решает, как спросить: кнопками или открытым вопросом.
    ask: reminders.Ask = reminders.Ask.NOTHING


def dedup_key(chat_id: int, message_id: int) -> str:
    """Ключ дедупликации для Telegram.

    Идентификатор сообщения, а не хэш содержимого: две одинаковые мысли подряд
    — законный случай, а повторная доставка одного апдейта — нет. ADR-0003
    формулирует дедупликацию как «содержимое плюс окно времени», и цель там
    названа прямо: защита от двойной доставки. Здесь та же цель достигается
    точно, без окна и без ложных срабатываний.
    """
    return f"telegram:{chat_id}:{message_id}"


def audio_path(audio_dir: Path, chat_id: int, message_id: int) -> str:
    """Куда ляжет файл голосового.

    Путь считается до вставки и из тех же данных, что и ключ дедупликации:
    `entries` не правится (инвариант 3), поэтому имя файла должно быть известно
    в момент вставки, когда идентификатора записи ещё нет.
    """
    return str(audio_dir / f"{chat_id}_{message_id}{VOICE_SUFFIX}")


async def capture_text(
    uow: CaptureUnitOfWork,
    message: IncomingText,
    *,
    is_private: bool,
    timezone: str,
    now: datetime,
) -> CaptureResult:
    """Принять текстовую запись и, если это напоминание, завести его."""
    entry_id = await uow.add_entry(
        EntryDraft(
            source=EntrySource.TELEGRAM,
            kind=EntryKind.TEXT,
            raw_text=message.text,
            is_private=is_private,
            timezone=timezone,
            dedup_key=dedup_key(message.chat_id, message.message_id),
            meta=_meta(
                message.chat_id,
                message.message_id,
                message.ack_message_id,
                answers=message.answers,
            ),
        )
    )
    if entry_id is None:
        return CaptureResult(reply="", duplicate=True)

    # Индексация — в той же транзакции (инвариант 7). Текст записи есть уже
    # сейчас, в отличие от голосовой, поэтому искать её можно будет сразу.
    # Приватная запись индексируется тоже: у неё будут чанки и полнотекст,
    # не будет только вектора (инвариант 5).
    await uow.enqueue(INDEX, {"entry_id": entry_id})

    # Разметка — там же и тем же способом. Приватной записи задание не ставится:
    # у неё разметки не бывает вовсе (инвариант 5), и задание было бы холостым.
    # Настоящий отбор всё равно делает обработчик — он собирает пачку запросом,
    # в котором приватность и так есть; здесь мы экономим пустой проход, а не
    # обеспечиваем приватность.
    if not is_private:
        await uow.enqueue(ENRICH, {"entry_id": entry_id})

    reminder = await reminders.schedule(
        uow,
        entry_id=entry_id,
        text=message.text,
        chat_id=message.chat_id,
        timezone=timezone,
        now=now,
    )

    if reminder is not None:
        # Сюда попадают все исходы: заведённое напоминание, признание «не понял,
        # когда» и вопрос к записи без слова «напомни». Каждый обязан быть
        # виден — см. `reminders.schedule`.
        return CaptureResult(reply=reminder.message, entry_id=entry_id, ask=reminder.ask)

    return CaptureResult(
        reply="Записал приватно." if is_private else "Записал.",
        entry_id=entry_id,
    )


async def capture_voice(
    uow: CaptureUnitOfWork,
    message: IncomingVoice,
    *,
    is_private: bool,
    timezone: str,
    audio_dir: Path,
) -> CaptureResult:
    """Принять голосовое и поставить задание на расшифровку.

    Приватный режим сюда не доходит: адаптер обязан отклонить голосовое раньше.
    Проверка продублирована в `core/stt` — там, где проходит граница наружу.
    """
    entry_id = await uow.add_entry(
        EntryDraft(
            source=EntrySource.TELEGRAM,
            kind=EntryKind.VOICE,
            audio_path=audio_path(audio_dir, message.chat_id, message.message_id),
            duration_sec=message.duration_sec,
            is_private=is_private,
            timezone=timezone,
            dedup_key=dedup_key(message.chat_id, message.message_id),
            meta=_meta(
                message.chat_id,
                message.message_id,
                message.ack_message_id,
                file_id=message.file_id,
                answers=message.answers,
            ),
        )
    )
    if entry_id is None:
        return CaptureResult(reply="", duplicate=True)

    await uow.enqueue(
        TRANSCRIBE,
        {
            "entry_id": entry_id,
            "chat_id": message.chat_id,
            "ack_message_id": message.ack_message_id,
        },
    )
    return CaptureResult(reply="Расшифровываю…", entry_id=entry_id)


def _meta(
    chat_id: int,
    message_id: int,
    ack_message_id: int,
    *,
    file_id: str | None = None,
    answers: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Служебное от адаптера. `ack_message_id` — то сообщение, которое правится.

    `answers` — связь с вопросом раскачки, на который эта запись отвечает
    (`vision.md` §5.9). Лежит здесь, а не отдельной таблицей и не колонкой,
    по той же причине, по которой здесь лежит `ack_message_id`: связка с
    перепиской — служебное поле записи, а `entries` не правится (инвариант 3),
    так что записать её можно только в момент вставки.
    """
    meta: dict[str, object] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "ack_message_id": ack_message_id,
    }
    if file_id is not None:
        meta["file_id"] = file_id
    if answers is not None:
        meta["answers"] = dict(answers)
    return meta
