"""Что должно работать:

1. пачка уходит **одним вызовом на прогон**, а не вызовом на запись
2. прогонов ровно два — состояние берётся из их расхождения
3. приватная запись в пачку не попадает и остаётся в архиве
4. тема, названная обоими прогонами, заводится; названная одним — нет
5. одна и та же новая тема в одной пачке заводится один раз, а не дважды
6. размеченная запись больше в пачку не попадает
7. записи размечаются той моделью, которой размечались, — провенанс на месте
8. запись из мысли и события получает **две единицы**, а не одну метку
9. единицы уходят вместе с разметкой, когда запись делают приватной

Тексты берутся из **живого архива** по номерам записей: разметка разбирает
живую речь с ошибками расшифровки, и «похожая» строка из головы ломает код
не так, как ломает настоящая (вывод из `docs/status.md`, 16 и 18 августа).

Требует поднятого Postgres и живого архива: `just up`.
"""

from typing import Any

import pytest
from sqlalchemy import Select, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.enrich import repository, service
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.topic import EntryTopic, Topic
from brain.storage.models.unit import EntryUnit
from tests import archive as archive_module
from tests.fakes import ScriptedModel, StubEmbedder, answer, answer_units

pytestmark = pytest.mark.integration

# Записи живого архива, на которых гоняется разметка. Выбраны не случайно:
# 26 — голосовая, где расшифровка съела слово, 31 — самая длинная запись
# архива, 8 — событие со временем, из-за которого в типах появился `event`.
LIVE_ENTRIES = (26, 31, 8)


async def _load_live(
    url: str, entries: list[archive_module.Entry], numbers: tuple[int, ...]
) -> dict[int, int]:
    """Положить живые записи в одноразовую базу. Вернуть «номер в архиве → id»."""
    engine = create_async_engine(url)
    mapping: dict[int, int] = {}
    try:
        for number in numbers:
            source = archive_module.entry(entries, number)
            async with engine.begin() as connection:
                entry_id = await connection.scalar(
                    insert(Entry)
                    .values(
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind.TEXT,
                        raw_text=source.text,
                        is_private=source.is_private,
                        timezone=source.timezone,
                        dedup_key=f"live:{number}",
                        meta={},
                    )
                    .returning(Entry.id)
                )
            mapping[number] = int(entry_id or 0)
    finally:
        await engine.dispose()
    return mapping


async def _private(url: str, text: str) -> int:
    """Приватная запись. В живом архиве таких нет ни одной — создаём свою."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.TEXT,
                    raw_text=text,
                    is_private=True,
                    timezone="Europe/Moscow",
                    dedup_key="private:1",
                    meta={},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _rows(url: str, statement: Select[Any]) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in await connection.execute(statement)]
    finally:
        await engine.dispose()


async def test_batch_is_one_call_per_run(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Три записи — два вызова, а не шесть. Разметка идёт пачкой (ADR-0011)."""
    ids = await _load_live(migrated_database, archive, LIVE_ENTRIES)
    model = ScriptedModel([answer(*[(entry_id, "thought", []) for entry_id in ids.values()])])

    outcome = await service.annotate_pending(model, StubEmbedder(), limit=25)

    assert outcome.annotated == 3
    assert model.calls == service.RUNS == 2
    # В одном промпте — все три записи: пачка, а не три отдельных вопроса.
    assert all(f"[{entry_id}]" in model.prompts[0] for entry_id in ids.values())


async def test_private_entry_is_not_annotated_but_stays(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Инвариант 5: приватная запись остаётся в архиве, но разметки не получает."""
    ids = await _load_live(migrated_database, archive, (26,))
    private_id = await _private(migrated_database, "это никуда не уходит")

    model = ScriptedModel([answer((ids[26], "task", []))])
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    assert f"[{private_id}]" not in model.prompts[0], "приватный текст ушёл в модель"
    annotated = await _rows(migrated_database, select(Annotation.source_entry_id))
    assert [row[0] for row in annotated] == [ids[26]]
    # Сама запись цела — приватность не значит удаление.
    kept = await _rows(migrated_database, select(Entry.id).where(Entry.id == private_id))
    assert kept == [(private_id,)]


async def test_topic_needs_both_runs(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Тема из одного прогона теряется — цена, названная в ADR-0011."""
    ids = await _load_live(migrated_database, archive, (31,))
    entry_id = ids[31]

    model = ScriptedModel(
        [
            answer((entry_id, "thought", ["разработка бота", "монетизация"])),
            answer((entry_id, "thought", ["разработка бота"])),
        ]
    )
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    # Базовые темы заводятся миграцией и записи-источника не имеют, поэтому
    # спрашиваются только те, что завела разметка (ADR-0011, «Обновление»).
    topics = await _rows(
        migrated_database, select(Topic.name).where(Topic.source_entry_id.is_not(None))
    )
    assert [row[0] for row in topics] == ["разработка бота"]


async def test_same_new_topic_in_one_batch_is_created_once(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Две записи предложили одну тему — в списке она должна быть одна.

    Проверка перед вставкой здесь не спасает: обе записи разбираются в одной
    пачке и обе видят пустой список тем.
    """
    ids = await _load_live(migrated_database, archive, (26, 31))
    both = [(entry_id, "task", ["подготовка к CV"]) for entry_id in ids.values()]
    model = ScriptedModel([answer(*both)])

    await service.annotate_pending(model, StubEmbedder(), limit=25)

    topics = await _rows(
        migrated_database,
        select(Topic.id, Topic.name).where(Topic.source_entry_id.is_not(None)),
    )
    assert len(topics) == 1
    links = await _rows(migrated_database, select(EntryTopic.source_entry_id))
    assert len(links) == 2


async def test_annotated_entry_is_not_taken_again(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Второе задание находит пустую пачку — и это норма, а не сбой."""
    ids = await _load_live(migrated_database, archive, (8,))
    model = ScriptedModel([answer((ids[8], "event", []))])

    first = await service.annotate_pending(model, StubEmbedder(), limit=25)
    second = await service.annotate_pending(model, StubEmbedder(), limit=25)

    assert first.annotated == 1
    assert second.annotated == 0
    assert model.calls == 2, "пустая пачка не должна стоить вызова"


async def test_disagreement_is_stored_with_both_versions(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Запись 8 — «во вторник в 9:00 экзамен»: ровно та граница задача/событие."""
    ids = await _load_live(migrated_database, archive, (8,))
    entry_id = ids[8]

    model = ScriptedModel([answer((entry_id, "task", [])), answer((entry_id, "event", []))])
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    stored = await _rows(
        migrated_database,
        select(Annotation.type, Annotation.alternative_type, Annotation.state, Annotation.model),
    )
    assert stored == [
        (
            AnnotationType.TASK,
            AnnotationType.EVENT,
            AnnotationState.TWO_HYPOTHESES,
            "stub-flash-lite",
        )
    ]


async def test_unannotated_count_falls_to_zero(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """`/status` считает отставание по данным — как и `just reenrich`."""
    ids = await _load_live(migrated_database, archive, LIVE_ENTRIES)
    assert await repository.unannotated_count() == 3

    model = ScriptedModel([answer(*[(entry_id, "thought", []) for entry_id in ids.values()])])
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    assert await repository.unannotated_count() == 0


async def test_entry_gets_a_unit_per_type_not_one_label(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Главное, ради чего написан ADR-0012.

    Запись 8 живого архива — «во вторник в 9:00 у меня экзамен по ГАИ» плюс
    «я пока не чувствую себя уверенно». Событие и мысль в одной записи;
    до ADR-0012 вторая половина не доходила до владельца вовсе.

    Цитата начала берётся **из живого текста**, а не придумывается: цитата
    из головы найдётся не там или не найдётся вовсе — ровно та ошибка,
    из-за которой напоминания дважды чинились в Фазе 1.
    """
    ids = await _load_live(migrated_database, archive, (8,))
    entry_id = ids[8]
    text = archive_module.entry(archive, 8).text
    second_half = text[text.index(".") + 2 :][:20]

    model = ScriptedModel([answer_units(entry_id, ("начало", "event"), (second_half, "thought"))])
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    units = await _rows(
        migrated_database,
        select(
            EntryUnit.ordinal, EntryUnit.type, EntryUnit.span_start, EntryUnit.span_end
        ).order_by(EntryUnit.ordinal),
    )
    assert [row[1] for row in units] == [AnnotationType.EVENT, AnnotationType.THOUGHT]
    # Единицы покрывают запись целиком и встык — дыра означала бы потерянный текст.
    assert units[0][2] == 0
    assert units[0][3] == units[1][2]
    assert units[-1][3] == len(text)

    annotation = await repository.latest_annotation(entry_id)
    assert annotation is not None
    assert await repository.unit_types_of(annotation.id) == [
        AnnotationType.EVENT,
        AnnotationType.THOUGHT,
    ]


async def test_units_go_away_with_their_annotation(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Кнопка «сделать приватной» удаляет разметку — единицы обязаны уйти с ней.

    Держались бы они внешним ключом — падала бы вся ретроактивная приватность,
    то есть путь инварианта 5. Каскад проверяется здесь, а не в модели: то,
    что написано в `ondelete`, значит что-то только на живой базе.
    """
    ids = await _load_live(migrated_database, archive, (8,))
    entry_id = ids[8]
    model = ScriptedModel([answer((entry_id, "event", []))])
    await service.annotate_pending(model, StubEmbedder(), limit=25)

    assert await _rows(migrated_database, select(EntryUnit.id)) != []

    removed = await repository.drop_annotations(entry_id)

    assert removed == 1
    assert await _rows(migrated_database, select(EntryUnit.id)) == []
