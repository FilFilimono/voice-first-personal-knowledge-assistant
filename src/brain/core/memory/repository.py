"""Чтение и запись всего, что не входит в транзакцию приёма.

Отдельно от `unit_of_work.py` намеренно: там одна транзакция, в которой нельзя
ошибиться, здесь — обычные чтения и вставки производных записей. Смешивать
их значит потерять из виду то место, где живёт инвариант 7.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, insert, select, update

from brain.core.ports import Transcription
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.private_mode import PrivateModeChange
from brain.storage.models.reminder import Reminder
from brain.storage.models.transcript import Transcript


async def is_private_mode() -> bool:
    """Включён ли приватный режим сейчас.

    Текущее состояние — последняя строка журнала. Никогда не переключали —
    значит выключено: приватность включается осознанно, а не по умолчанию.
    """
    async with session_scope() as session:
        latest = await session.scalar(
            select(PrivateModeChange.enabled).order_by(PrivateModeChange.id.desc()).limit(1)
        )
    return bool(latest)


async def switch_private_mode(*, enabled: bool) -> None:
    """Записать переключение режима."""
    async with session_scope() as session:
        await session.execute(insert(PrivateModeChange).values(enabled=enabled))


async def get_entry(entry_id: int) -> Entry | None:
    """Запись по идентификатору. Воркер получает в задании только его."""
    async with session_scope() as session:
        return await session.get(Entry, entry_id)


async def save_transcript(entry_id: int, transcription: Transcription) -> None:
    """Сохранить расшифровку как производную запись (инвариант 4)."""
    async with session_scope() as session:
        await session.execute(
            insert(Transcript).values(
                source_entry_id=entry_id,
                text=transcription.text,
                provider=transcription.provider,
                model=transcription.model,
                latency_ms=transcription.latency_ms,
            )
        )


async def get_transcript(entry_id: int) -> str | None:
    """Текущая расшифровка записи — последняя по времени.

    Перепрогон другой моделью добавляет строку, а не правит старую, поэтому
    «текущая» это всегда «последняя», а не «единственная».
    """
    async with session_scope() as session:
        text: str | None = await session.scalar(
            select(Transcript.text)
            .where(Transcript.source_entry_id == entry_id)
            .order_by(Transcript.id.desc())
            .limit(1)
        )
    return text


def is_private_now() -> ColumnElement[bool]:
    """Приватна ли запись **сейчас** — выражение для запроса, а не значение.

    До Фазы 3 ответом была колонка `entries.is_private`, и этого хватало.
    Теперь приватность включается задним числом кнопкой «сделать приватной»,
    а `entries` не правится (инвариант 3) — значит, состояние живёт в двух
    местах сразу: в колонке и в последней поправке.

    Это ровно та цена, которую назвал ADR-0003 («актуальная запись — не
    `SELECT * FROM entries`»), и он же сказал, чем платить: читать состояние
    в одном месте. Место — здесь. Всё, что решает, уходить ли тексту наружу,
    обязано спрашивать эту функцию, а не колонку: индексация, поиск, разметка.

    Обратной поправки нет, поэтому «последняя строка» сводится к «есть хоть
    одна». Появится «снять приватность» — здесь понадобится именно последняя,
    и это единственное место, которое придётся поправить.
    """
    return Entry.is_private | (
        select(EntryOverride.id)
        .where(
            EntryOverride.source_entry_id == Entry.id,
            EntryOverride.action == OverrideAction.MAKE_PRIVATE,
        )
        .exists()
    )


async def is_entry_private(entry_id: int) -> bool:
    """Приватна ли запись сейчас — значением, а не выражением.

    Нужна воркеру: у него на руках объект `Entry`, и соблазн спросить
    `entry.is_private` велик. Колонка отвечает на другой вопрос — «была ли
    запись приватной в момент приёма», — и с Фазы 3 это уже не одно и то же.
    """
    async with session_scope() as session:
        private = await session.scalar(select(is_private_now()).where(Entry.id == entry_id))
    return bool(private)


async def entry_text(entry_id: int) -> tuple[str, bool] | None:
    """Текущий текст записи и её приватность. `None`, если текста ещё нет.

    Текст берётся так же, как везде: расшифровка, если она есть, иначе
    `raw_text`. У голосовой записи до расшифровки текста нет вовсе — и это
    законное состояние, а не ошибка: индексировать её просто нечем, задание
    придёт заново, когда расшифровка появится.

    Приватность возвращается рядом с текстом намеренно. Тот, кто собирается
    считать эмбеддинг, обязан узнать про неё из того же запроса, а не сходить
    за ней отдельно и забыть. И считается она **сейчас** (`is_private_now`),
    а не по колонке: с Фазы 3 приватность включается задним числом.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(func.coalesce(latest_transcript(), Entry.raw_text), is_private_now()).where(
                    Entry.id == entry_id
                )
            )
        ).first()
    if row is None or row[0] is None:
        return None
    return str(row[0]), bool(row[1])


async def get_reminder(reminder_id: int) -> Reminder | None:
    async with session_scope() as session:
        return await session.get(Reminder, reminder_id)


async def entry_by_ack_message(chat_id: int, ack_message_id: int) -> tuple[int, str] | None:
    """Запись, чьё подтверждение приёма — это сообщение, и её текущий текст.

    Так находится запись, к которой относится ответ владельца на вопрос
    «когда напомнить?»: вопрос — это отредактированное подтверждение приёма,
    а его идентификатор лежит в `meta` с момента вставки. Отдельная таблица
    ожидающих вопросов не нужна, и кнопки переживают перезапуск бота.

    Текст берётся так же, как везде: расшифровка, если она есть, иначе
    `raw_text` — у голосовой записи своего текста нет.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Entry.id, func.coalesce(latest_transcript(), Entry.raw_text))
                .where(
                    Entry.meta["chat_id"].astext == str(chat_id),
                    Entry.meta["ack_message_id"].astext == str(ack_message_id),
                )
                .order_by(Entry.id.desc())
                .limit(1)
            )
        ).first()
    if row is None or row[1] is None:
        return None
    return int(row[0]), str(row[1])


def latest_transcript() -> Any:
    """Последняя расшифровка записи — подзапрос, а не join.

    Публичный, потому что нужен и ретривалу: «текущий текст записи» считается
    везде одинаково, и второй такой подзапрос рядом однажды разъедется с этим.

    Перепрогон другой моделью добавляет строку, поэтому «текущая» это всегда
    «последняя», и join дал бы столько строк, сколько было прогонов.
    """
    return (
        select(Transcript.text)
        .where(Transcript.source_entry_id == Entry.id)
        .order_by(Transcript.id.desc())
        .limit(1)
        .scalar_subquery()
    )


async def has_reminder(entry_id: int) -> bool:
    """Заводили ли уже напоминание по этой записи.

    Нужно повторному выполнению задания: расшифровка сохраняется отдельно
    от напоминания, и без этой проверки второй проход завёл бы второе.
    """
    async with session_scope() as session:
        found = await session.scalar(
            select(Reminder.id).where(Reminder.source_entry_id == entry_id).limit(1)
        )
    return found is not None


async def mark_reminder_fired(reminder_id: int) -> bool:
    """Отметить напоминание отправленным.

    Возвращает `False`, если оно уже было отмечено: задание может выполниться
    дважды (ретрай после падения на полпути), и второе напоминание об одном
    и том же раздражает сильнее, чем отсутствие первого.
    """
    async with session_scope() as session:
        updated: int | None = await session.scalar(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.fired_at.is_(None))
            .values(fired_at=datetime.now(UTC))
            .returning(Reminder.id)
        )
    return updated is not None


async def audio_paths_past_retention(days: int) -> list[str]:
    """Пути к аудио, которому пора исчезнуть.

    Строку записи это не трогает: `entries` не правится (инвариант 3).
    Удаляется файл, `audio_path` остаётся указывать в пустоту — отсутствующий
    файл читается как «удалено по ретеншену». Цена такого решения записана
    в ADR-0003: зачистка колонки потребовала бы привилегированной роли.
    """
    threshold = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as session:
        result = await session.scalars(
            select(Entry.audio_path).where(
                Entry.kind == EntryKind.VOICE,
                Entry.audio_path.is_not(None),
                Entry.created_at < threshold,
            )
        )
    return [path for path in result if path]
