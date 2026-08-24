"""Чтение и запись всего, что не входит в транзакцию приёма.

Отдельно от `unit_of_work.py` намеренно: там одна транзакция, в которой нельзя
ошибиться, здесь — обычные чтения и вставки производных записей. Смешивать
их значит потерять из виду то место, где живёт инвариант 7.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, insert, select, update

from brain.config import get_settings
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

    Текст **рабочий** (`working_text()`), а не показываемый: отсюда он уходит
    в чанки и эмбеддинги, то есть в индекс, а индекс обязан стоять на том же
    тексте, над которым считаются отрезки (ADR-0015, ось 3). У голосовой записи
    до расшифровки текста нет вовсе — и это законное состояние, а не ошибка:
    индексировать её просто нечем, задание придёт заново, когда расшифровка
    появится.

    Приватность возвращается рядом с текстом намеренно. Тот, кто собирается
    считать эмбеддинг, обязан узнать про неё из того же запроса, а не сходить
    за ней отдельно и забыть. И считается она **сейчас** (`is_private_now`),
    а не по колонке: с Фазы 3 приватность включается задним числом.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(working_text(), is_private_now()).where(Entry.id == entry_id)
            )
        ).first()
    if row is None or row[0] is None:
        return None
    return str(row[0]), bool(row[1])


async def get_reminder(reminder_id: int) -> Reminder | None:
    async with session_scope() as session:
        return await session.get(Reminder, reminder_id)


async def reminder_of(entry_id: int) -> Reminder | None:
    """Напоминание, заведённое по записи. Нужно перерисовке подтверждения.

    Подтверждение приёма живёт в Telegram, прочитать его нельзя — только
    заменить. Значит, всё, что там было, собирается заново, и строка
    «⏰ Напомню 24.08 в 19:40» берётся отсюда, а не пересчитывается из текста:
    «через 3 минуты», пересчитанное через полминуты, дало бы **другое время**,
    и владелец увидел бы обещание, которого система не давала.
    """
    async with session_scope() as session:
        found = await session.scalars(
            select(Reminder)
            .where(Reminder.source_entry_id == entry_id)
            .order_by(Reminder.id.desc())
            .limit(1)
        )
        return found.first()


async def entry_by_ack_message(chat_id: int, ack_message_id: int) -> tuple[int, str] | None:
    """Запись, чьё подтверждение приёма — это сообщение, и её текущий текст.

    Так находится запись, к которой относится ответ владельца на вопрос
    «когда напомнить?»: вопрос — это отредактированное подтверждение приёма,
    а его идентификатор лежит в `meta` с момента вставки. Отдельная таблица
    ожидающих вопросов не нужна, и кнопки переживают перезапуск бота.

    Текст **рабочий**: из него разбирается ответ «когда напомнить» и по нему
    считается смещение, а не показывается строка владельцу (ADR-0015, ось 3).
    У голосовой записи своего текста нет — берётся расшифровка.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Entry.id, working_text())
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


def latest_clean_transcript() -> Any:
    """Вычищенный текст последней расшифровки. NULL, пока чистка не прошла.

    Берётся из той же строки, что и `latest_transcript()`, и по тому же
    правилу «последняя это последняя»: чистка живёт колонкой в расшифровке,
    а не отдельной таблицей, поэтому расходиться им негде.

    Пустая строка приравнивается к NULL, и это не педантизм. Чистит модель,
    и на коротком «ок» она вполне вернёт `""` или пробел — а `coalesce`
    считает пустую строку значением и покажет владельцу пустоту вместо
    записи. Деградация обязана быть на дословный текст, а не на ничто,
    поэтому «пусто» решается здесь, а не в каждом месте показа.
    """
    return (
        select(func.nullif(func.btrim(Transcript.clean_text), ""))
        .where(Transcript.source_entry_id == Entry.id)
        .order_by(Transcript.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def display_text() -> Any:
    """Текст, который видит владелец. Вычищенный, если он есть.

    Единственное место, где решается вопрос «что показать», — как
    `is_private_now()` для приватности. Второе такое выражение рядом однажды
    разъедется с этим, и разъедется молча.

    Три ступени, а не две: у текстовой записи расшифровки нет вовсе, у только
    что принятой голосовой нет ещё и чистки. Обе ступени вниз — законные
    состояния, а не ошибки.
    """
    return func.coalesce(latest_clean_transcript(), latest_transcript(), Entry.raw_text)


def working_text() -> Any:
    """Текст, с которым работает система: разметка, отрезки, чанки, индекс.

    Пока дословный. Переключение на вычищенный — настройка
    `PRESENT_WORKING_TEXT`, и включается она **после** замера чистки,
    а не вместе с ней (ADR-0015, ось 3).

    **Переключать сегодня нечем, и это надо знать до, а не после.** Команды
    полного перепрогона в проекте нет. `just reindex` и `just reenrich` её
    не заменяют: первая отбирает записи **без чанков или без вектора**, вторая
    — только **неразмеченное** (`reenrich --stale` берёт разметку до ADR-0012).
    Уже посчитанного не касается ни одна.

    Значит, флаг, переключённый сегодня, оставит архив проиндексированным
    и размеченным над дословным текстом, а `digest/loops.create_from_entry`
    и `just backfill-commitments` начнут резать **вычищенный** теми же старыми
    отрезками — то есть дела и вопросы петель покажут чужие куски. Команда
    полного перепрогона пишется к моменту переключения, после замера чистки.

    Правило, по которому место выбирает между этим выражением и `display_text()`:
    всё, что режется по отрезку (`span_start`/`span_end`), обязано брать
    рабочий текст — отрезки посчитаны над ним. Всё, что целиком уходит
    на экран, берёт `display_text()`.
    """
    if get_settings().present_working_text == "clean":
        return func.coalesce(latest_clean_transcript(), latest_transcript(), Entry.raw_text)
    return func.coalesce(latest_transcript(), Entry.raw_text)


@dataclass(frozen=True, slots=True)
class CleanupTarget:
    """Расшифровка, которую предстоит вычистить, — вместе с приватностью.

    Приватность едет рядом с текстом по тому же правилу, что и в `entry_text()`
    и в `ports.EntryText`: тот, кто собирается отправить текст наружу, обязан
    узнать про неё из того же запроса, а не сходить отдельно и забыть.
    Голосовое в приватном режиме не принимается вовсе, но приватность включают
    **задним числом** кнопкой — и тогда расшифровка уже есть.
    """

    transcript_id: int
    # Дословный: чистится именно он. Вычищенная версия ложится рядом.
    text: str
    clean_text: str | None
    clean_model: str | None
    is_private: bool


async def transcript_to_clean(entry_id: int) -> CleanupTarget | None:
    """Последняя расшифровка записи и всё, что нужно решить про её чистку.

    `None` — расшифровки ещё нет. Законное состояние: задание чистки могло
    опередить расшифровку после ручного перезапуска очереди.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    Transcript.id,
                    Transcript.text,
                    Transcript.clean_text,
                    Transcript.clean_model,
                    is_private_now(),
                )
                .join(Entry, Entry.id == Transcript.source_entry_id)
                .where(Transcript.source_entry_id == entry_id)
                .order_by(Transcript.id.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return CleanupTarget(
        transcript_id=int(row[0]),
        text=str(row[1]),
        clean_text=row[2],
        clean_model=row[3],
        is_private=bool(row[4]),
    )


async def display_of(entry_id: int) -> str | None:
    """Текст записи, каким его видит владелец: вычищенный, если он есть.

    Значением, а не выражением — как `is_entry_private()` рядом с
    `is_private_now()`. Нужна воркеру: он перерисовывает подтверждение приёма
    после чистки, и текст ему нужен один и тот же с тем, что покажет разметка.
    """
    async with session_scope() as session:
        shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    return None if shown is None else str(shown)


async def save_clean_text(transcript_id: int, clean: str, *, model: str) -> None:
    """Записать вычищенную версию рядом с дословной.

    UPDATE здесь не нарушает инварианта 3: он про `entries`, где хранится
    сказанное владельцем. `transcripts` — производная запись, и вычищенная
    версия дописывается в ту же строку, а не заводит вторую: иначе
    «последняя расшифровка» перестала бы быть одной и той же для показа
    и для отрезков разметки (ADR-0012), а вот это уже разъехалось бы молча.
    """
    async with session_scope() as session:
        await session.execute(
            update(Transcript)
            .where(Transcript.id == transcript_id)
            .values(clean_text=clean, clean_model=model, cleaned_at=datetime.now(UTC))
        )


def latest_transcript_id() -> Any:
    """Идентификатор той же самой последней расшифровки.

    Нужен разметке по единицам: единица хранится отрезком, а отрезок обязан
    помнить, над каким текстом посчитан (ADR-0012). Отдельным подзапросом,
    а не вторым полем в `latest_transcript()`, потому что скалярный подзапрос
    возвращает один столбец, — но правило «последняя это последняя» здесь
    то же самое, и менять их надо вместе.
    """
    return (
        select(Transcript.id)
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
