"""Чтение и запись разметки.

Отдельно от `service.py` намеренно: там правило, по которому из двух прогонов
получается одна разметка, здесь — SQL. Смешав их, мы получили бы файл, где
самое интересное тонет среди вставок.

**Приватность здесь спрашивается всегда через `is_private_now()`.** Запись,
сделанную приватной задним числом, колонка `entries.is_private` по-прежнему
считает обычной — таблица не правится (инвариант 3). Разметка такой записи
удаляется кнопкой и не должна появиться снова при следующем проходе.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select

from brain.core.digest import commitments
from brain.core.enrich.units import MergedUnit
from brain.core.memory.repository import (
    display_text,
    is_private_now,
    latest_transcript_id,
    working_text,
)
from brain.core.ports import EntryText
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.reminder import Reminder
from brain.storage.models.topic import EntryTopic, Topic, normalize
from brain.storage.models.unit import EntryUnit


@dataclass(frozen=True, slots=True)
class KnownTopic:
    """Тема, как её видит разметка."""

    id: int
    name: str
    slug: str
    # Вектор названия мог не посчитаться: провайдер эмбеддингов лежал в момент
    # заведения темы. Отбор кандидатов обязан знать об этом — тему без вектора
    # он не найдёт никогда и должен показывать её всегда.
    embedding_missing: bool = False


@dataclass(frozen=True, slots=True)
class NewTopic:
    """Тема, которой ещё нет. Заводится только с обоснованием (ADR-0011)."""

    name: str
    reason: str | None
    embedding: list[float] | None
    embedding_model: str | None


async def entries_needing_annotation(limit: int) -> list[EntryText]:
    """Записи, которые ещё не размечены. Считается по данным, а не по очереди.

    Так же устроены `just reindex` и `just requeue-transcripts`, и по той же
    причине: задание может исчезнуть — из-за исчерпанных попыток, из-за падения
    между записью и постановкой, — а данные остаются.

    Приватные не попадают сюда вовсе: их разметки не бывает (инвариант 5,
    ADR-0002). Дверь в `core/llm` проверит это ещё раз, но приватная запись
    не должна доходить до двери — она должна не попадать в пачку.

    Текст **рабочий**: над ним модель считает границы смысловых единиц
    (ADR-0012), и отрезки потом режутся из него же. Показать владельцу
    вычищенный вариант можно где угодно, кроме этого места, — иначе отрезки
    и текст разъедутся на длину вычищенного (ADR-0015, ось 3).
    """
    text_of = working_text()
    annotated = select(Annotation.id).where(Annotation.source_entry_id == Entry.id).exists()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, text_of)
                .where(text_of.is_not(None), ~is_private_now(), ~annotated)
                .order_by(Entry.id)
                .limit(limit)
            )
        ).all()

    return [EntryText(entry_id=int(row[0]), text=str(row[1]), is_private=False) for row in rows]


async def entries_for_reannotation(entry_ids: list[int]) -> list[EntryText]:
    """Записи по явному списку — для перепрогона уже размеченного.

    Отдельно от `entries_needing_annotation` намеренно: та отбирает
    **неразмеченное**, и это правильно для обычной работы. Перепрогон —
    другая операция, и 19 августа 2026 попытка сделать её через ту же выборку
    поставила 37 заданий, которые не нашли бы себе работы.

    **Приватность проверяется и здесь.** Список идентификаторов приходит
    снаружи и мог бы протащить приватную запись мимо фильтра, который стоит
    в обычной выборке. Инвариант 5 не должен зависеть от того, каким путём
    запись попала в пачку.
    """
    if not entry_ids:
        return []

    text_of = working_text()
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, text_of)
                .where(Entry.id.in_(entry_ids), text_of.is_not(None), ~is_private_now())
                .order_by(Entry.id)
            )
        ).all()

    return [EntryText(entry_id=int(row[0]), text=str(row[1]), is_private=False) for row in rows]


async def stale_entry_ids() -> list[int]:
    """Записи, чья текущая разметка сделана до ADR-0012 — без смысловых единиц.

    Из единиц выводятся дела Фазы 4 и предложение завести петлю, поэтому такая
    разметка выглядит рабочей и молча не даёт половине системы работать.
    """
    latest = (
        select(Annotation.source_entry_id, func.max(Annotation.id).label("id"))
        .group_by(Annotation.source_entry_id)
        .subquery()
    )
    async with session_scope() as session:
        rows = await session.scalars(
            select(latest.c.source_entry_id)
            .where(~select(EntryUnit.id).where(EntryUnit.annotation_id == latest.c.id).exists())
            .order_by(latest.c.source_entry_id)
        )
    return [int(entry_id) for entry_id in rows]


async def transcript_ids(entry_ids: list[int]) -> dict[int, int | None]:
    """Какой расшифровкой сейчас представлена каждая запись.

    Отдельным запросом, а не полем в `EntryText`: `EntryText` — это граница
    ядра (`core/ports.py`), и её видят ещё расшифровка с поиском, которым
    идентификатор расшифровки не нужен. Один лишний запрос на пачку дешевле
    поля, которое всем приходится игнорировать.
    """
    if not entry_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, latest_transcript_id()).where(Entry.id.in_(entry_ids))
            )
        ).all()
    return {int(row[0]): (int(row[1]) if row[1] is not None else None) for row in rows}


async def known_topics() -> list[KnownTopic]:
    """Все темы архива, в порядке появления."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.id, Topic.name, Topic.slug, Topic.embedding.is_(None)).order_by(
                    Topic.id
                )
            )
        ).all()
    return [
        KnownTopic(id=int(r[0]), name=str(r[1]), slug=str(r[2]), embedding_missing=bool(r[3]))
        for r in rows
    ]


async def save_annotation(
    *,
    entry_id: int,
    type_: AnnotationType,
    alternative_type: AnnotationType | None,
    state: AnnotationState,
    entities: list[str],
    provider: str,
    model: str,
    existing_topic_ids: list[int],
    new_topics: list[NewTopic],
    units: list[MergedUnit],
    transcript_id: int | None,
    text: str,
) -> int:
    """Сохранить разметку записи со всем, что из неё следует. Одной транзакцией.

    Вместе с разметкой пишутся новые темы, привязка тем к записи и дела,
    выведенные из единиц типа «задача». Раздельными транзакциями это писать
    нельзя: упав между ними, мы получили бы разметку без тем и никакого следа
    о том, что так вышло, — или тему, которую никто не приписал. Это та же дыра,
    что с расшифровкой без задания на индексацию, только тише.

    Привязка тем перезаписывается целиком: перепрогон обязан быть идемпотентным
    ровно так же, как пересчёт чанков. Дела — наоборот, не перезаписываются:
    у них своя жизнь после разметки, и закрытое дело не должно воскресать
    при `just reenrich` (ADR-0013, ось 3).

    `text` нужен делам: они хранят снимок своего куска, а не только отрезок.
    """
    async with session_scope() as session:
        annotation_id = await session.scalar(
            insert(Annotation)
            .values(
                source_entry_id=entry_id,
                type=type_,
                alternative_type=alternative_type,
                state=state,
                entities=entities,
                provider=provider,
                model=model,
            )
            .returning(Annotation.id)
        )
        if annotation_id is None:  # pragma: no cover — INSERT ... RETURNING без строки
            raise RuntimeError("разметка не вставилась")

        if units:
            # Единицы привязаны к этой строке разметки, а не к записи: перепрогон
            # добавляет разметку новой строкой (как расшифровка), и его единицы
            # ложатся рядом со старыми, а не поверх них. «Текущие единицы» —
            # это единицы последней разметки, и спрашиваются они так же.
            await session.execute(
                insert(EntryUnit),
                [
                    {
                        "source_entry_id": entry_id,
                        "source_transcript_id": transcript_id,
                        "annotation_id": annotation_id,
                        "ordinal": ordinal,
                        "span_start": unit.start,
                        "span_end": unit.end,
                        "type": unit.type,
                        "alternative_type": unit.alternative_type,
                        "state": unit.state,
                    }
                    for ordinal, unit in enumerate(units)
                ],
            )

            # Дела заводятся здесь же и по тем же единицам. Порядок важен:
            # `create_in` читает единицы из этой транзакции, а не из базы.
            await commitments.create_in(
                session, entry_id=entry_id, annotation_id=int(annotation_id), text=text
            )

        topic_ids = list(existing_topic_ids)
        for topic in new_topics:
            slug = normalize(topic.name)
            # Тема могла появиться в этой же пачке — на соседней записи.
            # Поэтому не «проверим и вставим», а вставка с разрешением конфликта:
            # проверка перед вставкой пропустила бы обе.
            existing = await session.scalar(select(Topic.id).where(Topic.slug == slug))
            if existing is not None:
                topic_ids.append(int(existing))
                continue

            created = await session.scalar(
                insert(Topic)
                .values(
                    source_entry_id=entry_id,
                    name=topic.name,
                    slug=slug,
                    reason=topic.reason,
                    embedding=topic.embedding,
                    embedding_model=topic.embedding_model,
                )
                .returning(Topic.id)
            )
            if created is not None:
                topic_ids.append(int(created))

        await session.execute(delete(EntryTopic).where(EntryTopic.source_entry_id == entry_id))
        if topic_ids:
            await session.execute(
                insert(EntryTopic),
                [
                    {
                        "source_entry_id": entry_id,
                        "topic_id": topic_id,
                        "annotation_id": annotation_id,
                    }
                    for topic_id in dict.fromkeys(topic_ids)
                ],
            )

    return int(annotation_id)


@dataclass(frozen=True, slots=True)
class ReplyRow:
    """Всё, что нужно, чтобы пересобрать сообщение владельцу."""

    entry_id: int
    chat_id: int | None
    ack_message_id: int | None
    created_at: datetime
    timezone: str
    kind: str
    text: str
    reminder_at: datetime | None
    reminder_text: str | None


async def rows_for_reply(entry_ids: list[int]) -> list[ReplyRow]:
    """Данные для сообщения о готовой разметке.

    Собирается запросом, а не дописыванием к тому, что уже показано: текст
    подтверждения живёт в Telegram, прочитать его нельзя — только заменить.
    Значит, всё, что там было, надо уметь восстановить из базы.
    """
    if not entry_ids:
        return []

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Entry.id,
                    Entry.meta["chat_id"].astext,
                    Entry.meta["ack_message_id"].astext,
                    Entry.created_at,
                    Entry.timezone,
                    Entry.kind,
                    # Показ, а не работа: эта строка целиком уходит в сообщение
                    # владельцу и по отрезкам не режется. Значит, вычищенный
                    # вариант, если он есть (ADR-0015, ось 3).
                    display_text(),
                    Reminder.fire_at,
                    Reminder.text,
                )
                .outerjoin(Reminder, Reminder.source_entry_id == Entry.id)
                .where(Entry.id.in_(entry_ids))
                .order_by(Entry.id)
            )
        ).all()

    return [
        ReplyRow(
            entry_id=int(row[0]),
            chat_id=int(row[1]) if row[1] else None,
            ack_message_id=int(row[2]) if row[2] else None,
            created_at=row[3],
            timezone=str(row[4]),
            kind=str(row[5]),
            text=str(row[6] or ""),
            reminder_at=row[7],
            reminder_text=row[8],
        )
        for row in rows
    ]


async def latest_annotation(entry_id: int) -> Annotation | None:
    """Текущая разметка записи — последняя по времени.

    Перепрогон другой моделью добавляет строку, а не правит старую, поэтому
    «текущая» это всегда «последняя», а не «единственная».
    """
    async with session_scope() as session:
        found: Annotation | None = await session.scalar(
            select(Annotation)
            .where(Annotation.source_entry_id == entry_id)
            .order_by(Annotation.id.desc())
            .limit(1)
        )
    return found


@dataclass(frozen=True, slots=True)
class Regeneralization:
    """Что осталось от списка тем после переобобщения."""

    topics_after: int
    removed: list[str]


async def entries_since_last(purpose: str) -> int:
    """Сколько записей появилось после последнего удавшегося вызова с таким назначением.

    Временем прошлого прохода служит `llm_calls`, а не своя колонка и не журнал
    очереди: строка там пишется внутри двери наружу и забыть её невозможно
    (ADR-0004), а задание может исчезнуть, исчерпав попытки. Считать по данным —
    то же правило, по которому живут `just reindex` и `just reenrich`.
    """
    async with session_scope() as session:
        since = await session.scalar(
            select(func.max(LlmCall.created_at)).where(
                LlmCall.purpose == purpose, LlmCall.succeeded.is_(True)
            )
        )
        statement = select(func.count()).select_from(Entry)
        if since is not None:
            statement = statement.where(Entry.created_at > since)
        return int(await session.scalar(statement) or 0)


async def recent_entries(limit: int) -> list[EntryText]:
    """Последние записи с текстом — пачка, на которой считается переобобщение.

    Приватные не попадают сюда вовсе, как и в разметку: их текст наружу
    не уходит (инвариант 5), и тем у них не бывает.
    """
    text_of = working_text()
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, text_of)
                .where(text_of.is_not(None), ~is_private_now())
                .order_by(Entry.id.desc())
                .limit(limit)
            )
        ).all()
    return [
        EntryText(entry_id=int(row[0]), text=str(row[1]), is_private=False)
        for row in reversed(rows)
    ]


async def replace_topics(
    assignments: dict[int, list[str]],
    *,
    vectors: dict[str, list[float]],
    embedding_model: str,
) -> Regeneralization:
    """Переписать привязку тем целиком и убрать темы, которыми никто не пользуется.

    Одной транзакцией: упав посередине, мы получили бы архив, у части которого
    темы новые, а у части старые, — и отличить одно от другого было бы нечем.

    **Базовые темы не удаляются.** У них нет записи-источника, потому что
    их завела миграция, а не запись: пустая базовая тема — это список, которому
    пока не из чего наполниться, а не мусор.
    """
    async with session_scope() as session:
        by_slug = {
            str(row[1]): int(row[0])
            for row in (await session.execute(select(Topic.id, Topic.slug))).all()
        }

        for entry_id, names in assignments.items():
            topic_ids: list[int] = []
            for name in names:
                slug = normalize(name)
                if slug not in by_slug:
                    created = await session.scalar(
                        insert(Topic)
                        .values(
                            source_entry_id=entry_id,
                            name=name,
                            slug=slug,
                            reason="заведена переобобщением: несколько записей про одно",
                            embedding=vectors.get(name),
                            embedding_model=embedding_model if name in vectors else None,
                        )
                        .returning(Topic.id)
                    )
                    if created is None:  # pragma: no cover — INSERT ... RETURNING без строки
                        continue
                    by_slug[slug] = int(created)
                topic_ids.append(by_slug[slug])

            annotation_id = await session.scalar(
                select(Annotation.id)
                .where(Annotation.source_entry_id == entry_id)
                .order_by(Annotation.id.desc())
                .limit(1)
            )
            if annotation_id is None:
                # Запись ещё не размечена — привязать тему не к чему.
                # Её разберёт разметка, а следующий проход подхватит.
                continue

            await session.execute(delete(EntryTopic).where(EntryTopic.source_entry_id == entry_id))
            if topic_ids:
                await session.execute(
                    insert(EntryTopic),
                    [
                        {
                            "source_entry_id": entry_id,
                            "topic_id": topic_id,
                            "annotation_id": int(annotation_id),
                        }
                        for topic_id in dict.fromkeys(topic_ids)
                    ],
                )

        used = select(EntryTopic.topic_id).distinct().scalar_subquery()
        removed = (
            await session.scalars(
                delete(Topic)
                .where(Topic.source_entry_id.is_not(None), Topic.id.not_in(used))
                .returning(Topic.name)
            )
        ).all()
        left = await session.scalar(select(func.count()).select_from(Topic)) or 0

    return Regeneralization(topics_after=int(left), removed=[str(name) for name in removed])


async def unit_types_of(annotation_id: int) -> list[AnnotationType]:
    """Типы единиц этого разбора, по порядку и без повторов.

    Это и есть «сводный набор типов записи» из ADR-0012. Отдельной колонки
    под него нет намеренно: свод целиком выводится из единиц, а вторая копия
    однажды разойдётся с первой — ровно как разошлась бы таблица перевода
    типов, от которой отказался ADR-0011.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(EntryUnit.type)
            .where(EntryUnit.annotation_id == annotation_id)
            .order_by(EntryUnit.ordinal)
        )
    seen: list[AnnotationType] = []
    for type_ in rows:
        if type_ not in seen:
            seen.append(type_)
    return seen


async def topics_of(entry_id: int) -> list[str]:
    """Имена тем записи — то, что показывают владельцу."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(Topic.name)
            .join(EntryTopic, EntryTopic.topic_id == Topic.id)
            .where(EntryTopic.source_entry_id == entry_id)
            .order_by(Topic.id)
        )
    return list(rows)


async def drop_annotations(entry_id: int) -> int:
    """Убрать разметку записи целиком — вместе с привязкой тем.

    Зовётся кнопкой «сделать приватной»: у приватной записи аннотаций не бывает
    (ADR-0002, инвариант схемы 4). Темы, заведённые этой записью, при этом
    остаются: имя темы — не текст записи, и удаление сломало бы привязки других
    записей. Это названо вслух в тексте, который видит владелец.
    """
    async with session_scope() as session:
        await session.execute(delete(EntryTopic).where(EntryTopic.source_entry_id == entry_id))
        # Единицы уходят вместе с разметкой каскадом, а не строкой здесь:
        # удаление разметки бывает в двух местах, и второе про единицы забыло
        # бы. Поймано тестом, а не рассуждением.
        removed = (
            await session.scalars(
                delete(Annotation)
                .where(Annotation.source_entry_id == entry_id)
                .returning(Annotation.id)
            )
        ).all()
    return len(removed)


async def counts_by_state() -> dict[AnnotationState, int]:
    """Сколько записей в каком состоянии — по текущей разметке каждой.

    Нужно `/status`: «две гипотезы» и «не понял» — это очередь на вечерний
    разбор, и её размер должен быть виден с телефона, а не только в базе.
    """
    latest = (
        select(
            Annotation.source_entry_id,
            func.max(Annotation.id).label("id"),
        )
        .group_by(Annotation.source_entry_id)
        .subquery()
    )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Annotation.state, func.count())
                .join(latest, latest.c.id == Annotation.id)
                .group_by(Annotation.state)
            )
        ).all()
    return {AnnotationState(row[0]): int(row[1]) for row in rows}


async def unannotated_count() -> int:
    """Сколько записей ещё ждут разметки."""
    text_of = working_text()
    annotated = select(Annotation.id).where(Annotation.source_entry_id == Entry.id).exists()

    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Entry)
            .where(text_of.is_not(None), ~is_private_now(), ~annotated)
        )
    return int(count or 0)
