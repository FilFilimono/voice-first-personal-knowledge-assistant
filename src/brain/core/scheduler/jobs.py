"""Что делает воркер, когда достаёт задание из очереди.

В задании лежат только идентификаторы (ADR-0005), поэтому первое действие
любого обработчика — сходить в базу за записью. Содержимое в очереди
не дублируется намеренно: очередь не должна становиться вторым местом,
где лежит текст.

Про ошибки. Их здесь два сорта, и обходятся с ними по-разному:

- **временные** (провайдер не ответил, сеть отвалилась) — исключение уходит
  наверх, задание повторяется с нарастающей задержкой. Счётчик попыток живёт
  в базе, поэтому перезапуск воркера ретраи не сбрасывает;
- **окончательные** (приватная запись, слишком длинное аудио) — повторять
  бессмысленно, результат будет тот же. Владельцу объясняется, задание
  закрывается успешно.

Различать их обязательно: без этого приватная запись, попавшая сюда по ошибке,
пять раз подряд постучалась бы во внешний сервис.
"""

import logging
from pathlib import Path

from brain.core import stt
from brain.core.memory import audio as audio_store
from brain.core.memory import repository
from brain.core.ports import AudioSource, Notifier, Transcriber
from brain.core.stt.errors import AudioTooLongError, PrivateContentError

logger = logging.getLogger(__name__)

VOICE_MIME = "audio/ogg"

ENTRY_GONE = "Запись потерялась — такого быть не должно, посмотрите логи."
TOO_LONG = "Голосовое слишком длинное, я такое не расшифровываю. Разбейте на части."
PRIVATE_LEAK = "Голосовое в приватном режиме не расшифровывается. Наружу ничего не ушло."


async def transcribe(
    *,
    entry_id: int,
    chat_id: int,
    ack_message_id: int,
    transcriber: Transcriber,
    audio_source: AudioSource,
    notifier: Notifier,
    max_duration_sec: int,
) -> None:
    """Расшифровать голосовое и показать результат вместо подтверждения."""
    entry = await repository.get_entry(entry_id)
    if entry is None or entry.audio_path is None:
        logger.error("задание на расшифровку без записи: entry_id=%s", entry_id)
        await notifier.edit(chat_id, ack_message_id, ENTRY_GONE)
        return

    existing = await repository.get_transcript(entry_id)
    if existing is not None:
        # Задание выполнилось дважды: расшифровка уже есть, платить за неё
        # второй раз незачем. Подтверждение всё равно приводим в порядок —
        # первый проход мог упасть как раз на правке сообщения.
        await notifier.edit(chat_id, ack_message_id, existing)
        return

    audio = await _audio_bytes(entry.audio_path, entry.meta.get("file_id"), audio_source)

    try:
        transcription = await stt.transcribe_entry(
            transcriber,
            audio=audio,
            mime_type=VOICE_MIME,
            is_private=entry.is_private,
            duration_sec=entry.duration_sec,
            max_duration_sec=max_duration_sec,
        )
    except PrivateContentError:
        logger.exception("приватная запись дошла до расшифровки: entry_id=%s", entry_id)
        await notifier.edit(chat_id, ack_message_id, PRIVATE_LEAK)
        return
    except AudioTooLongError:
        logger.warning("голосовое длиннее потолка: entry_id=%s", entry_id)
        await notifier.edit(chat_id, ack_message_id, TOO_LONG)
        return

    await repository.save_transcript(entry_id, transcription)
    await notifier.edit(chat_id, ack_message_id, transcription.text)


async def _audio_bytes(path: str, file_id: object, audio_source: AudioSource) -> bytes:
    """Взять файл с диска, а если его там нет — скачать и положить.

    Скачивание живёт здесь, а не в обработчике сообщения: между приходом
    сообщения и долговечной записью должно пройти как можно меньше времени,
    а сетевая операция ещё и падает — тут она попадает под ретраи очереди.
    """
    local = Path(path)
    if local.exists():
        return local.read_bytes()

    if not isinstance(file_id, str):
        raise RuntimeError(f"нечего скачивать: ни файла {path}, ни file_id в записи")

    audio = await audio_source.download(file_id)
    await audio_store.save(path, audio)
    return audio


async def remind(*, reminder_id: int, chat_id: int, notifier: Notifier) -> None:
    """Отправить напоминание, если оно ещё не отправлено."""
    reminder = await repository.get_reminder(reminder_id)
    if reminder is None:
        logger.error("задание на напоминание без напоминания: id=%s", reminder_id)
        return

    # Отметка ставится до отправки и одним запросом: два напоминания об одном
    # и том же раздражают сильнее, чем отсутствие одного.
    if not await repository.mark_reminder_fired(reminder_id):
        logger.info("напоминание %s уже отправлено", reminder_id)
        return

    await notifier.send(chat_id, f"⏰ {reminder.text}")


async def clean_audio(*, retention_days: int) -> int:
    """Удалить аудио, пережившее ретеншен. Текст остаётся навсегда."""
    paths = await repository.audio_paths_past_retention(retention_days)
    removed = await audio_store.delete(paths)
    if removed:
        logger.info("удалено файлов аудио по ретеншену: %s", removed)
    return removed
