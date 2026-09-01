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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from brain.config import get_settings
from brain.core import digest, present, quota, stt
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
from brain.core.retrieval import links
from brain.core.retrieval.indexing import index_entry
from brain.core.scheduler import reminders
from brain.core.scheduler.queue import CLEANUP, ENRICH, INDEX, SHOW_TRANSCRIPT
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

# Насколько свежим должно быть подтверждение приёма, чтобы его ещё правили.
#
# Правило одно на всех, кто дописывает что-то в это сообщение: и на разметку,
# и на чистку. Править сообщение недельной давности значит переписывать
# переписку задним числом — о такой правке никто не просил, и владелец увидит
# её как сообщение, изменившееся само.
#
# У чистки к этому добавляется своя беда, и она хуже косметической. Под квотой
# чистка переставляет себя на сутки вперёд (`_postpone_cleanup`), а перерисовка
# собирает сообщение заново — вместе со строкой «⏰ Напомню в 19:40». Для
# напоминания, которое за эти сутки давно сработало, это обещание прошлого
# в будущем времени. Поэтому по истечении окна вычищенный текст **сохраняется**
# (он нужен показу везде: поиску, сводке, вечернему разбору), а сообщение
# не трогается вовсе.
ACK_WINDOW = timedelta(hours=1)

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
        #
        # Разбирается напоминание по **дословному** тексту (отрезки и время
        # считаются над ним), а показывается вычищенный, если он уже есть:
        # повтор задания не повод показать владельцу «э» второй раз.
        reminder = await _schedule_reminder(entry, chat_id, existing)
        await _show(
            notifier,
            chat_id,
            ack_message_id,
            entry.id,
            await repository.display_of(entry_id) or existing,
            reminder,
        )
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

    stored = await _store_transcript(entry, chat_id, ack_message_id, transcription)
    if stored.deferred:
        # Показ отложен намеренно: ждём чистку. Мгновенно показанный мусор
        # хуже небольшой задержки — весь смысл ADR-0015 в этом. Показывать
        # будет задание `show-transcript`, если чистка не успеет первой.
        return

    await _show(notifier, chat_id, ack_message_id, entry.id, transcription.text, stored.reminder)


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


@dataclass(frozen=True, slots=True)
class _Stored:
    """Что получилось из расшифровки и надо ли показывать её прямо сейчас."""

    reminder: reminders.ScheduleOutcome | None
    # Показ отложен: чистка в пути, и владелец не должен увидеть дословный
    # вариант, если она успеет. Не отложен — чистки не будет (выключена или
    # запись приватна), и показывать надо сразу, как до Фазы 5.
    deferred: bool


async def _store_transcript(
    entry: Entry, chat_id: int, ack_message_id: int, transcription: Transcription
) -> _Stored:
    """Сохранить расшифровку и всё, что из неё следует, — одной транзакцией.

    Вместе с расшифровкой в базу уходят её следствия: задания на индексацию,
    разметку, чистку и на показ по истечении ожидания, плюс напоминание, если
    запись им является. Раздельными транзакциями это писать нельзя — упав
    между ними, мы получили бы расшифровку, которую невозможно найти,
    и никакого следа о том, что так вышло. Это та же дыра, что с пропавшими
    заданиями расшифровки 16 августа, только тише: там было видно отсутствие
    ответа, здесь не видно ничего.

    **Разметка ставится с той же задержкой, что и показ.** Она рисует то же
    самое сообщение (`_show_annotations`) и берёт текст выражением
    `display_text()`. Опередив чистку, она показала бы дословный вариант —
    и вся задача оказалась бы напрасной, причём незаметно: и разметка,
    и чистка при этом работают правильно, просто пришли не в том порядке.
    Задержка не бесплатна — разбор записи появляется на полминуты позже,
    — но это ровно тот обмен, который ADR-0015 называет ценой.
    """
    already_reminded = await repository.has_reminder(entry.id)
    is_private = await repository.is_entry_private(entry.id)

    settings = get_settings()
    # Приватность спрашивается заново, а не берётся из колонки: её включают
    # задним числом, и тогда расшифровка уже есть, а чистка ещё нет.
    cleaning = settings.cleanup_enabled and not is_private
    wait = timedelta(seconds=settings.cleanup_wait_sec) if cleaning else None
    payload = {"entry_id": entry.id, "chat_id": chat_id, "ack_message_id": ack_message_id}

    async with capture_transaction() as uow:
        await uow.add_transcript(entry.id, transcription)
        await uow.enqueue(INDEX, {"entry_id": entry.id})

        if cleaning:
            await uow.enqueue(CLEANUP, payload)
            await uow.enqueue(SHOW_TRANSCRIPT, payload, execute_after=wait)

        # Разметка ставится здесь, а не при приёме: при приёме у голосовой
        # записи текста ещё нет, а размечать нечем.
        if not is_private:
            await uow.enqueue(ENRICH, {"entry_id": entry.id}, execute_after=wait)

        if already_reminded:
            return _Stored(reminder=None, deferred=cleaning)

        # Напоминание разбирается здесь, а не при приёме: при приёме текста
        # ещё нет. Голосом «напомни через 3 минуты» говорят чаще, чем текстом,
        # — на ходу только так и получается.
        reminder = await reminders.schedule(
            uow,
            entry_id=entry.id,
            chat_id=chat_id,
            text=transcription.text,
            timezone=entry.timezone,
            now=datetime.now(UTC),
        )

    return _Stored(reminder=reminder, deferred=cleaning)


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


async def cleanup(
    *,
    entry_id: int,
    chat_id: int,
    ack_message_id: int,
    model: LanguageModel,
    notifier: Notifier,
) -> None:
    """Вычистить расшифровку и показать владельцу то, что получилось.

    Отдельным заданием от расшифровки — ADR-0015, ось 2: промпт расшифровки
    не трогается, а отказ чистки не стоит записи. Ретраи здесь тоже свои:
    упавшая чистка повторяется, не задевая ни текста, ни напоминания.
    """
    target = await repository.transcript_to_clean(entry_id)
    if target is None:
        # Законное состояние: задание опередило расшифровку после ручного
        # перезапуска очереди. Задание закрывается — новое поставит расшифровка.
        logger.info("чистить нечего: у записи %s ещё нет расшифровки", entry_id)
        return

    if target.clean_text is not None:
        # Задание выполняется второй раз: текст уже вычищен, платить за него
        # снова незачем. А вот перерисовать надо — прошлый проход мог упасть
        # **между** записью в базу и правкой сообщения, и тогда владелец
        # остался бы с «Принял» навсегда: задание показа по сроку тоже молчит,
        # увидев готовый `clean_text`. Повторная одинаковая правка Telegram'у
        # безразлична, её глотает `TelegramNotifier._edit`.
        logger.info("запись %s уже вычищена — только перерисовываю", entry_id)
        await _redraw(notifier, entry_id=entry_id, chat_id=chat_id, ack_message_id=ack_message_id)
        return

    if target.is_private:
        # Приватность включили задним числом, а расшифровка уже была.
        # Наружу текст не уходит (инвариант 5), и это не сбой задания:
        # повторять бессмысленно, результат будет тот же.
        logger.warning("запись %s стала приватной — чистка отменена", entry_id)
        return

    now = datetime.now(UTC)
    if not await present.cosmetic_allowed(now):
        await _postpone_cleanup(entry_id, chat_id, ack_message_id, now)
        return

    clean = await present.clean_one(
        model, entry_id=entry_id, text=target.text, is_private=target.is_private
    )
    if clean is None:
        # Модель ответила не тем. Ретрай стоил бы ещё одного запроса под квотой
        # ради того же ответа, а деградация уже написана: показ уйдёт дословным.
        return

    await repository.save_clean_text(target.transcript_id, clean, model=model.model)
    await _redraw(notifier, entry_id=entry_id, chat_id=chat_id, ack_message_id=ack_message_id)


async def _postpone_cleanup(
    entry_id: int, chat_id: int, ack_message_id: int, now: datetime
) -> None:
    """Переставить чистку на время сброса квоты вместо того, чтобы умереть.

    Ретраи очереди — пять попыток с ростом задержки, то есть около получаса,
    а суточная квота сбрасывается через часы (ADR-0014, «Цена решения»).
    Без переноса косметическое задание исчерпало бы попытки и ушло в `failed`,
    где ждало бы `just requeue` руками — при том, что чинить нечего и всё
    пройдёт само к полуночи по тихоокеанскому времени.
    """
    resets_at = quota.resets_at(now)
    async with capture_transaction() as uow:
        await uow.enqueue(
            CLEANUP,
            {"entry_id": entry_id, "chat_id": chat_id, "ack_message_id": ack_message_id},
            execute_after=resets_at - now,
        )
    logger.info(
        "чистка записи %s отложена до %s: квота", entry_id, resets_at.isoformat(timespec="minutes")
    )


async def show_transcript(
    *, entry_id: int, chat_id: int, ack_message_id: int, notifier: Notifier
) -> None:
    """Показать расшифровку, когда ждать чистку больше нельзя.

    Это и есть «ожидание» из ADR-0015 — отложенное задание, а не сон
    в обработчике: спящий обработчик держит слот воркера и не переживает
    перезапуск. Здесь ожидание не стоит ничего, пока идёт.

    Дождались чистки — задание молчит: сообщение уже перерисовано, и второй
    раз показывать то же самое незачем. Не дождались — показывается дословный
    вариант, а чистка заменит его, когда пройдёт. Молчать над готовой
    расшифровкой полчаса было бы хуже.
    """
    target = await repository.transcript_to_clean(entry_id)
    if target is None:
        logger.info("показывать нечего: у записи %s нет расшифровки", entry_id)
        return

    if target.clean_text is not None:
        return

    logger.info("чистка записи %s не успела — показываю дословный вариант", entry_id)
    await _redraw(notifier, entry_id=entry_id, chat_id=chat_id, ack_message_id=ack_message_id)


async def _redraw(notifier: Notifier, *, entry_id: int, chat_id: int, ack_message_id: int) -> None:
    """Перерисовать подтверждение приёма — **тем же путём, каким его рисовали**.

    Голый `notifier.edit(chat_id, ack_message_id, clean_text)` здесь был бы
    ошибкой, и ошибкой тихой. Расшифровка показывается не одна: `_show` клеит
    к ней судьбу напоминания («⏰ Напомню 24.08 в 19:40», «⚠️ не понял, когда»)
    и в одной ветке вешает кнопки «да/нет». Заменив сообщение одним текстом,
    чистка стёрла бы и строку, и кнопки: владелец прочитал бы это как
    «напоминание не завелось», а на пропавший вопрос ответить было бы нечем.

    Разметка приходит своим заданием и рисует то же самое сообщение по-своему
    — с типом, темами и своими кнопками (`_show_annotations`). Если она уже
    успела, перерисовка идёт её путём: иначе чистка стёрла бы разбор ровно
    так же, как стёрла бы напоминание.

    Старое сообщение не трогается ни одной из двух веток — `ACK_WINDOW`, и там
    же объяснено, почему у чистки на это причина серьёзнее, чем у разметки.
    Пропуск пишется в лог: молчание, которого не видно в журнале, в этом
    проекте считается дырой.
    """
    entry = await repository.get_entry(entry_id)
    if entry is None:  # pragma: no cover — запись только что читали
        logger.error("перерисовывать нечего: запись %s исчезла", entry_id)
        return

    age = datetime.now(UTC) - entry.created_at
    if age > ACK_WINDOW:
        logger.info(
            "перерисовка записи %s пропущена: подтверждению приёма уже %s, "
            "вычищенный текст сохранён и виден везде, кроме этого сообщения",
            entry_id,
            age,
        )
        return

    if await enrichment.latest_annotation(entry_id) is not None:
        await _show_annotations(notifier, [entry_id])
        return

    shown = await repository.display_of(entry_id)
    if shown is None:  # pragma: no cover — расшифровка только что читалась
        logger.error("перерисовывать нечего: у записи %s нет текста", entry_id)
        return

    await _show(notifier, chat_id, ack_message_id, entry_id, shown, await _reminder_shown(entry))


async def _reminder_shown(entry: Entry) -> reminders.ScheduleOutcome | None:
    """Что владельцу уже сказали про напоминание этой записи.

    Время **заведённого** напоминания берётся из базы, а не разбирается заново:
    «через 3 минуты», пересчитанное через полминуты, дало бы обещание
    на полминуты позже настоящего.

    А вот **вопрос** «Похоже на напоминание: …? Поставить на 25.08 в 19:40?»
    нигде не хранится — ни текстом, ни временем, — и собрать его можно только
    разбором от текущего `now`. Значит, при перерисовке он может показать
    другое время, чем показал в первый раз: «в 19:40», спрошенное в 19:45,
    во второй раз уедет на завтра. На тридцати секундах ожидания разницы нет
    никогда; заметной она стала бы на перерисовке через часы — и именно её
    закрывает `ACK_WINDOW` в `_redraw`, а не эта функция. Здесь — знание
    о том, что такая точность у вопроса недостижима без хранения самого вопроса.
    """
    reminder = await repository.reminder_of(entry.id)
    # Разбирается **рабочий** текст: над ним считались и время, и отрезки.
    working = await repository.entry_text(entry.id)
    if working is None:  # pragma: no cover — расшифровка только что читалась
        return None

    return reminders.shown(
        working[0],
        fire_at=None if reminder is None else reminder.fire_at,
        now=datetime.now(UTC),
        timezone=entry.timezone,
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


async def index(
    *, entry_id: int, embedder: Embedder, notifier: Notifier, model: LanguageModel
) -> None:
    """Разрезать запись на чанки, посчитать вектора и показать связи.

    Индексация — задание молчаливое: владельцу о ней знать незачем, он о ней
    не просил. Наружу она проявляется только тем, что запись начинает
    находиться, — и `/status` показывает, сколько записей ещё ждут индекса.

    **Связи считаются здесь и не молчат.** Место выбрано не для удобства:
    связь считается вектором (инвариант 8), а вектор появляется ровно в этом
    задании и ни секундой раньше. Позже — в разметке — было бы поздно: `vision
    .md` §5.4 обещает связь **в момент записи**, а разметка приходит через
    минуту после чистки.
    """
    chunks = await index_entry(embedder, entry_id=entry_id)
    logger.info("запись %s проиндексирована: чанков %s", entry_id, chunks)

    try:
        await _show_links(entry_id, notifier=notifier, model=model)
    except Exception:
        # Связь дешевле потерять, чем переиндексировать запись. Исключение
        # отсюда ушло бы наверх, задание попало бы в ретраи — и пересчитало бы
        # вектора, уже посчитанные и оплаченные, ради косметического
        # сообщения. Тот же обмен, что у названий в `_name_fresh`.
        logger.exception("связи по записи %s не показаны", entry_id)


async def _show_links(entry_id: int, *, notifier: Notifier, model: LanguageModel) -> None:
    """Сказать владельцу, с чем перекликается только что сделанная запись.

    Три условия молчания, и каждое куплено:

    **Выключено настройкой.** `LINKS_ENABLED=false` — механизм срабатывает
    на каждой записи, и снимать его надо не правкой кода.

    **Запись не свежая.** Связь показывается только по свежему приёму —
    то же окно, что у перерисовки подтверждения. Без этого условия `just
    reindex` разослал бы связи по всему архиву разом: пересчёт индекса зовёт
    ровно это задание, и отличить пересчёт от приёма можно только по возрасту
    записи.

    **Связей нет.** Обычный исход, а не сбой: на архиве в 85 записей связь
    находится у каждой пятой.

    Сообщение уходит **отдельным и беззвучным**. Отдельным — потому что
    подтверждение приёма к этому моменту либо ещё дословное, либо уже занято
    разметкой, и дописать в него связь значило бы драться с двумя другими
    заданиями за одно и то же сообщение. Беззвучным — потому что владелец
    в этот момент и так смотрит в телефон: он только что сделал запись.
    """
    settings = get_settings()
    if not settings.links_enabled:
        return

    entry = await repository.get_entry(entry_id)
    if entry is None:  # pragma: no cover — запись только что индексировали
        return

    age = datetime.now(UTC) - entry.created_at
    if age > ACK_WINDOW:
        logger.info("связи по записи %s не показаны: приёму уже %s", entry_id, age)
        return

    chat_id = entry.meta.get("chat_id")
    if not isinstance(chat_id, int):
        logger.info("связи по записи %s некому показать: в записи нет чата", entry_id)
        return

    found = await links.related(entry_id, rules=links.from_settings(settings))
    if not found:
        return

    # Показывается **вычищенный** текст записи, если он уже есть: связь читается
    # рядом с ним, и «э-э-э» в одном сообщении с чистой цитатой выглядело бы
    # сбоем. Модель фразу пишет только при `RECALL_WRITTEN=true`.
    shown = await repository.display_of(entry_id) or ""
    await notifier.send_quietly(
        chat_id,
        await present.recall.link_text(
            model,
            text=shown,
            links=found,
            timezone=entry.timezone,
            now=datetime.now(UTC),
        ),
    )
    logger.info(
        "связи по записи %s показаны: %s",
        entry_id,
        ", ".join(f"{link.entry_id} ({link.similarity:.2f})" for link in found),
    )


async def enrich(
    *,
    entry_id: int,
    model: LanguageModel,
    titler: LanguageModel,
    embedder: Embedder,
    notifier: Notifier,
    limit: int,
    entry_ids: list[int] | None = None,
) -> int:
    """Разметить всё, что ждёт разметки. Вернуть число размеченных записей.

    Задание приходит на конкретную запись, а размечается пачка: обработчик
    забирает всё неразмеченное разом (ADR-0011). Поэтому второе задание,
    пришедшее следом, чаще всего не найдёт работы — и это нормально, а не сбой.

    `entry_ids` в нагрузке переключает задание на **перепрогон** явного списка.
    Без этой ветки `just reenrich --stale` ставил задания, которым нечего было
    делать: обычная выборка берёт неразмеченное, а перепрогон нужен как раз
    для размеченного (найдено разбором очереди 20 августа 2026).

    `titler` — **вторая модель, а не та же**: разметка и названия выбираются
    порознь (`TITLES_MODEL`, ADR-0015). Он обязателен в подписи намеренно: имей
    он умолчание, забытая проводка выглядела бы работающей — тот самый почерк,
    которым в проекте уже терялась `loops.is_stuck`.
    """
    outcome = await enrichment.annotate_pending(model, embedder, limit=limit, entry_ids=entry_ids)
    if outcome.annotated == 0:
        logger.info("разметка: пачку уже разобрало соседнее задание (запись %s)", entry_id)
        return 0

    await _show_annotations(notifier, outcome.entry_ids)
    await _name_fresh(titler)
    return int(outcome.annotated)


async def _name_fresh(titler: LanguageModel) -> None:
    """Назвать дела и петли, у которых названия ещё нет.

    **Пачка ограничена одним вызовом.** Без потолка первое же задание разметки
    после включения этой задачи утащило бы в модель весь накопленный архив —
    три запроса из двадцати суточных за раз, и это при живом темпе. Старое
    разгребается командой `just backfill-titles`, где владелец видит цену
    в запросах до того, как её платит.

    **Отказ названий не роняет разметку.** Исключение отсюда ушло бы наверх,
    задание разметки попало бы в ретраи — и разметило бы пачку второй раз,
    то есть заплатило бы двумя вызовами за уже сделанную работу. Названия
    дешевле потерять: их доставит следующая запись или команда.

    Под квотой названия молчат первыми, как чистка: `vision.md` §7 и ADR-0015,
    п. 6 — косметика отключается раньше расшифровки, напоминаний и разметки.
    """
    if not await present.cosmetic_allowed(datetime.now(UTC)):
        return

    try:
        await present.titles.name_open(titler, limit=present.titles.BATCH_LIMIT)
    except Exception:
        logger.exception("названия не проставлены — дела покажутся отрезком")


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
            # То же самое для привычки (`vision.md` §5.11): заводится
            # с подтверждения, как петля, а не молча — механизм, который сам
            # начинает напоминать каждый день, слишком дорого стоит при ошибке
            # распознавания.
            #
            # Признаков здесь два, а не один, как у петли: разметка нашла
            # привычку **и** по этой записи её ещё не завели. Второе спрашивается
            # у ядра, потому что кнопка живёт в переписке вечно, а нажатая
            # второй раз обязана не завести второе ежедневное сообщение.
            #
            # Считается **здесь, рядом с `offer_loop`**, а не в адаптере:
            # первая редакция 01.09.2026 считала его в `TelegramNotifier`,
            # и хотя инвариант 1 формально не нарушался (адаптер ходил в базу
            # через сервис ядра), место было неверное. Признак — часть замысла,
            # а не оформления: два отправителя разошлись бы в том, когда
            # предлагать, и разошлись бы молча.
            offer_habit=await digest.habits.proposal(row.entry_id) is not None,
        )


async def touch(
    *, notifier: Notifier, chat_id: int, timezone: str, model: LanguageModel
) -> list[str]:
    """Тик касаний. Возвращает слоты, по которым что-то ушло.

    Молчание — обычный исход, а не сбой: на молодом архиве сказать чаще всего
    нечего (ADR-0013). Почему это не молчание системы вообще — потому что
    каждый такой тик оставляет строку в журнале, и `/status` умеет объяснить,
    почему сводки сегодня не было.

    `model` пишет лид утренней сводки (ADR-0015, п. 5). Её отказ ничего
    не роняет: сводка уходит сухим рендером, и разбирается с этим сам слой
    представления — здесь ловить нечего.
    """
    sent = await digest.tick(
        now=datetime.now(UTC),
        timezone=timezone,
        chat_id=chat_id,
        notifier=notifier,
        model=model,
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
