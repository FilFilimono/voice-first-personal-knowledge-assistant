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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from brain.core import digest, stt
from brain.core import enrich as enrichment
from brain.core.enrich import regeneralize as regeneralization
from brain.core.memory import audio as audio_store
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import (
    AudioSource,
    Embedder,
    LanguageModel,
    Notifier,
    Transcriber,
    Transcription,
)
from brain.core.retrieval.indexing import index_entry
from brain.core.scheduler import reminders
from brain.core.scheduler.queue import ENRICH, INDEX
from brain.core.stt.errors import AudioTooLongError, PrivateContentError
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.reminder import Reminder

logger = logging.getLogger(__name__)

VOICE_MIME = "audio/ogg"

# Насколько напоминание должно опоздать, чтобы называть это просрочкой.
# Две минуты отделяют настоящее опоздание от обычной задержки очереди:
# в базе сработавшие вовремя отстают на десятые доли секунды, а то, что
# ждало поднятия воркера, — на десятки минут.
OVERDUE_AFTER = timedelta(minutes=2)

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
        # второй раз незачем. Но напоминание могло не завестись — первый проход
        # мог упасть между сохранением расшифровки и постановкой напоминания,
        # и тогда повтор это единственный шанс его завести.
        reminder = await _schedule_reminder(entry, chat_id, existing)
        await _show(notifier, chat_id, ack_message_id, entry.id, existing, reminder)
        return

    audio = await _audio_bytes(entry.audio_path, entry.meta.get("file_id"), audio_source)

    try:
        transcription = await stt.transcribe_entry(
            transcriber,
            audio=audio,
            mime_type=VOICE_MIME,
            # Приватность спрашивается заново, а не берётся из колонки: с Фазы 3
            # её включают задним числом, и `entries` при этом не меняется.
            is_private=await repository.is_entry_private(entry_id),
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

    reminder = await _store_transcript(entry, chat_id, transcription)
    await _show(notifier, chat_id, ack_message_id, entry.id, transcription.text, reminder)


# Значок перед судьбой напоминания. Разные намеренно: ⏰ значит «сделано»,
# ⚠️ — «услышал „напомни“, но не понял когда», ❓ — «спрашиваю, ответьте».
MARKS = {
    reminders.Ask.NOTHING: "⚠️",
    reminders.Ask.CONFIRM: "❓",
    reminders.Ask.WHEN: "❓",
}


async def _show(
    notifier: Notifier,
    chat_id: int,
    ack_message_id: int,
    entry_id: int,
    transcript: str,
    reminder: reminders.ScheduleOutcome | None,
) -> None:
    """Показать расшифровку и судьбу напоминания — одним сообщением.

    Голос — главный источник просьб без слова «напомни»: расшифровка теряет
    начало слова чаще, чем клавиатура. Поэтому вопрос задаётся и здесь,
    и ровно теми же кнопками, что в боте.
    """
    if reminder is None:
        await notifier.edit(chat_id, ack_message_id, transcript)
        return

    mark = "⏰" if reminder.created else MARKS[reminder.ask]
    text = f"{transcript}\n\n{mark} {reminder.message}"

    if reminder.ask is reminders.Ask.CONFIRM:
        await notifier.ask_yes_no(chat_id, ack_message_id, text, entry_id=entry_id)
        return

    await notifier.edit(chat_id, ack_message_id, text)


async def _store_transcript(
    entry: Entry, chat_id: int, transcription: Transcription
) -> reminders.ScheduleOutcome | None:
    """Сохранить расшифровку и всё, что из неё следует, — одной транзакцией.

    Вместе с расшифровкой в базу уходят два следствия: задание на индексацию
    и напоминание, если запись им является. Раздельными транзакциями это
    писать нельзя — упав между ними, мы получили бы расшифровку, которую
    невозможно найти, и никакого следа о том, что так вышло. Это та же дыра,
    что с пропавшими заданиями расшифровки 16 августа, только тише: там было
    видно отсутствие ответа, здесь не видно ничего.
    """
    already_reminded = await repository.has_reminder(entry.id)
    is_private = await repository.is_entry_private(entry.id)

    async with capture_transaction() as uow:
        await uow.add_transcript(entry.id, transcription)
        await uow.enqueue(INDEX, {"entry_id": entry.id})
        # Разметка ставится здесь, а не при приёме: при приёме у голосовой
        # записи текста ещё нет, а размечать нечем.
        if not is_private:
            await uow.enqueue(ENRICH, {"entry_id": entry.id})

        if already_reminded:
            return None

        # Напоминание разбирается здесь, а не при приёме: при приёме текста
        # ещё нет. Голосом «напомни через 3 минуты» говорят чаще, чем текстом,
        # — на ходу только так и получается.
        return await reminders.schedule(
            uow,
            entry_id=entry.id,
            chat_id=chat_id,
            text=transcription.text,
            timezone=entry.timezone,
            now=datetime.now(UTC),
        )


async def _schedule_reminder(
    entry: Entry, chat_id: int, text: str
) -> reminders.ScheduleOutcome | None:
    """Завести напоминание по расшифровке, если она им является.

    Повторно не заводит: задание может выполниться дважды, а два напоминания
    об одном и том же раздражают сильнее, чем отсутствие одного.
    """
    if await repository.has_reminder(entry.id):
        return None

    async with capture_transaction() as uow:
        return await reminders.schedule(
            uow,
            entry_id=entry.id,
            chat_id=chat_id,
            text=text,
            timezone=entry.timezone,
            now=datetime.now(UTC),
        )


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

    await notifier.send(chat_id, await _reminder_text(reminder))


async def _reminder_text(reminder: Reminder) -> str:
    """Текст напоминания, а к нему — признание, если оно опоздало.

    Просроченное напоминание не теряется: pgqueuer берёт задание, как только
    воркер поднимается, потому что срок у него уже наступил. Но приходит оно
    молча, и владелец читает «⏰ уйти домой» как «пора» — хотя пора было час
    назад. Так 17 августа напоминание на 20:00 пришло в 20:46 и выглядело
    обычным: воркер в этот момент лежал.

    Время показывается в таймзоне записи, а не сервера: напоминание про
    «19:40» должно и опоздать про 19:40.
    """
    late = datetime.now(UTC) - reminder.fire_at
    if late < OVERDUE_AFTER:
        return f"⏰ {reminder.text}"

    entry = await repository.get_entry(reminder.source_entry_id)
    timezone = ZoneInfo(entry.timezone) if entry is not None else UTC
    due = reminder.fire_at.astimezone(timezone)
    return f"⏰ {reminder.text}\n\n⚠️ Просрочено: должно было прийти {due:%d.%m в %H:%M}."


async def index(*, entry_id: int, embedder: Embedder) -> None:
    """Разрезать запись на чанки и посчитать вектора.

    Молчаливое задание: владельцу об индексации знать незачем, он о ней
    не просил. Наружу оно проявляется только тем, что запись начинает
    находиться, — и `/status` показывает, сколько записей ещё ждут индекса.
    """
    chunks = await index_entry(embedder, entry_id=entry_id)
    logger.info("запись %s проиндексирована: чанков %s", entry_id, chunks)


async def enrich(
    *,
    entry_id: int,
    model: LanguageModel,
    embedder: Embedder,
    notifier: Notifier,
    limit: int,
) -> int:
    """Разметить всё, что ждёт разметки. Вернуть число размеченных записей.

    Задание приходит на конкретную запись, а размечается пачка: обработчик
    забирает всё неразмеченное разом (ADR-0011). Поэтому второе задание,
    пришедшее следом, чаще всего не найдёт работы — и это нормально, а не сбой.
    """
    outcome = await enrichment.annotate_pending(model, embedder, limit=limit)
    if outcome.annotated == 0:
        logger.info("разметка: пачку уже разобрало соседнее задание (запись %s)", entry_id)
        return 0

    await _show_annotations(notifier, outcome.entry_ids)
    return int(outcome.annotated)


# Насколько свежей должна быть запись, чтобы бот дописал к подтверждению приёма
# разметку. Старая размечается молча, и это не экономия: править сообщение
# недельной давности значит переписывать переписку задним числом. Первый же
# проход разбирает весь накопленный архив — и всё это были бы правки старых
# сообщений, о которых никто не просил.
ACK_WINDOW = timedelta(hours=1)


async def _show_annotations(notifier: Notifier, entry_ids: list[int]) -> None:
    """Дописать разметку к подтверждению приёма — там, где это уместно.

    Сообщение пересобирается из данных целиком: текст подтверждения живёт
    в Telegram, прочитать его нельзя. Поэтому расшифровка, напоминание
    и разметка достаются из базы и складываются заново — см. `enrich/reply.py`.
    """
    rows = await enrichment.rows_for_reply(entry_ids)
    now = datetime.now(UTC)

    for row in rows:
        if row.chat_id is None or row.ack_message_id is None:
            continue
        if now - row.created_at > ACK_WINDOW:
            continue

        annotation = await enrichment.latest_annotation(row.entry_id)
        if annotation is None:  # pragma: no cover — только что размечали
            continue

        types = await enrichment.unit_types_of(annotation.id)
        shown = enrichment.Shown(
            # У голосовой записи показывается расшифровка — она и была
            # на этом месте до разметки. У текстовой — «Записал.»: повторять
            # владельцу его же слова незачем, он их только что набрал.
            body=row.text if row.kind == "voice" else enrichment.STORED,
            type=annotation.type,
            alternative_type=annotation.alternative_type,
            state=annotation.state,
            types=types,
            topics=await enrichment.topics_of(row.entry_id),
            reminder_at=row.reminder_at,
            reminder_text=row.reminder_text,
            timezone=row.timezone,
        )
        await notifier.show_annotation(
            row.chat_id,
            row.ack_message_id,
            enrichment.render(shown),
            entry_id=row.entry_id,
            # Модель нашла открытый вопрос — предлагаем завести петлю. Именно
            # предлагаем: петля заводится только нажатием (`vision.md` §5.2),
            # а спрашивать отдельным сообщением нельзя — одна запись стоит
            # одного уведомления.
            offer_loop=AnnotationType.LOOP in types,
        )


async def touch(*, notifier: Notifier, chat_id: int, timezone: str) -> list[str]:
    """Тик касаний. Возвращает слоты, по которым что-то ушло.

    Молчание — обычный исход, а не сбой: на молодом архиве сказать чаще всего
    нечего (ADR-0013). Почему это не молчание системы вообще — потому что
    каждый такой тик оставляет строку в журнале, и `/status` умеет объяснить,
    почему сводки сегодня не было.
    """
    sent = await digest.tick(
        now=datetime.now(UTC), timezone=timezone, chat_id=chat_id, notifier=notifier
    )
    if sent:
        logger.info("касания отправлены: %s", ", ".join(slot.value for slot in sent))
    return [slot.value for slot in sent]


async def clean_audio(*, retention_days: int) -> int:
    """Удалить аудио, пережившее ретеншен. Текст остаётся навсегда."""
    paths = await repository.audio_paths_past_retention(retention_days)
    removed = await audio_store.delete(paths)
    if removed:
        logger.info("удалено файлов аудио по ретеншену: %s", removed)
    return removed


async def regeneralize_topics(*, model: LanguageModel, embedder: Embedder) -> int:
    """Переобобщить темы, если накопилось достаточно записей. Иначе — не звать модель.

    Срабатывание считается по данным, а не по расписанию: расписание здесь
    только повод посмотреть. Проход на трёх новых записях стоил бы столько же,
    сколько на сорока, и не дал бы ничего — обобщать снова было бы не из чего.
    """
    accumulated = await regeneralization.due()
    if accumulated < regeneralization.EVERY:
        logger.info(
            "переобобщение пропущено: с прошлого прохода %s записей из %s",
            accumulated,
            regeneralization.EVERY,
        )
        return 0

    outcome = await regeneralization.regeneralize(model, embedder)
    return outcome.topics_after
