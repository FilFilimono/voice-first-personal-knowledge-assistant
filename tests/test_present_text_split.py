"""Показ берёт вычищенный текст, работа — дословный.

Разделение заведено ADR-0015: если чистка что-то потеряет, а поиск и отрезки
стоят на вычищенном, потерянное исчезнет молча и навсегда — оно останется
в дословном варианте и не найдётся. Поэтому рабочий текст переключается
настройкой и только после замера.

Фикстуры живут здесь, а не в `conftest.py`: расшифровка с двумя текстами нужна
пока только этому файлу, а фикстура в общем месте читается как обещание, что ею
пользуются многие.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from itertools import count

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain.config import get_settings
from brain.core.digest import evening
from brain.core.enrich import repository as enrich_repository
from brain.core.memory import repository as memory_repository
from brain.core.memory.repository import display_text, working_text
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.transcript import Transcript

pytestmark = pytest.mark.integration

MakeEntry = Callable[..., Awaitable[int]]


@pytest.fixture
async def session(migrated_database: str) -> AsyncGenerator[AsyncSession]:
    """Сессия к одноразовой базе интеграционных тестов.

    Своя, а не `session_scope()`: та открывает транзакцию и закрывает её вместе
    с сессией, а тесту нужно сначала вставить строки, а потом их прочитать
    в том же соединении.
    """
    engine = create_async_engine(migrated_database)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as opened:
            yield opened
    finally:
        await engine.dispose()


@pytest.fixture
def voice_entry_with_transcript(session: AsyncSession) -> MakeEntry:
    """Голосовая запись с расшифровкой. `clean=None` — чистка ещё не прошла.

    Голосовая, потому что чистить есть что только у речи: у текстовой записи
    дословный вариант и есть то, что написал владелец.
    """
    keys = count()

    async def make(*, verbatim: str, clean: str | None) -> int:
        entry_id = await session.scalar(
            insert(Entry)
            .values(
                source=EntrySource.TELEGRAM,
                kind=EntryKind.VOICE,
                raw_text=None,
                audio_path="var/audio/test.oga",
                is_private=False,
                timezone="Europe/Rome",
                dedup_key=f"present-{next(keys)}",
                meta={},
            )
            .returning(Entry.id)
        )
        await session.execute(
            insert(Transcript).values(
                source_entry_id=entry_id,
                text=verbatim,
                clean_text=clean,
                provider="stub",
                model="stub",
                latency_ms=1,
            )
        )
        await session.commit()
        return int(entry_id or 0)

    return make


async def test_display_prefers_clean_text(
    session: AsyncSession, voice_entry_with_transcript: MakeEntry
) -> None:
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    assert shown == "Надо посмотреть фильм."


async def test_working_stays_verbatim_by_default(
    session: AsyncSession, voice_entry_with_transcript: MakeEntry
) -> None:
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    used = await session.scalar(select(working_text()).where(Entry.id == entry_id))
    assert used == "э ну надо мне посмотреть фильм"


async def test_working_follows_setting_when_switched(
    session: AsyncSession,
    voice_entry_with_transcript: MakeEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройка переключает рабочий текст — иначе она непроверяема.

    Кэш настроек чистится руками: `Settings` заморожен, а `get_settings`
    под `lru_cache`, и без этого переменная окружения не доедет до выражения.
    """
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    monkeypatch.setenv("PRESENT_WORKING_TEXT", "clean")
    get_settings.cache_clear()

    used = await session.scalar(select(working_text()).where(Entry.id == entry_id))
    assert used == "Надо посмотреть фильм."


async def test_display_falls_back_when_cleanup_has_not_run(
    session: AsyncSession, voice_entry_with_transcript: MakeEntry
) -> None:
    """Чистка отстала — показывается дословный, а не пустота."""
    entry_id = await voice_entry_with_transcript(verbatim="надо посмотреть фильм", clean=None)
    shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    assert shown == "надо посмотреть фильм"


async def test_display_falls_back_when_cleanup_returned_nothing(
    session: AsyncSession, voice_entry_with_transcript: MakeEntry
) -> None:
    """Чистка вернула пустоту — показывается дословный, а не пустая строка.

    `coalesce` пустую строку считает значением, и на коротком «ок» модель
    вполне вернёт `""` или пробел. Владелец увидел бы пустое сообщение —
    ровно тот отказ, ради которого деградация и написана.
    """
    entry_id = await voice_entry_with_transcript(verbatim="ок", clean="   ")
    shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    assert shown == "ок"


# --- Пути целиком: выражение проверяется там, где им пользуются ---
#
# Четыре теста выше дёргают выражения напрямую, и этого мало. Во всей остальной
# сюите `clean_text` пуст, поэтому `display_text()` и `working_text()` дают одно
# и то же — и перепутанное выражение в любом из одиннадцати мест оставило бы
# сюиту зелёной. Тесты ниже идут от входа системы до того, что видит владелец,
# и покрывают оба направления в двух файлах, где перепутать проще всего.


async def _annotate(entry_id: int, *, text: str, state: AnnotationState) -> None:
    """Разметка записи — минимальная, только чтобы вечер её увидел."""
    await enrich_repository.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.THOUGHT,
        alternative_type=AnnotationType.TASK,
        state=state,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[],
        transcript_id=None,
        text=text,
    )


async def test_evening_shows_the_clean_version(voice_entry_with_transcript: MakeEntry) -> None:
    """Спорная запись доходит до вечернего разбора вычищенной."""
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    await _annotate(
        entry_id, text="э ну надо мне посмотреть фильм", state=AnnotationState.TWO_HYPOTHESES
    )

    waiting = await evening.disputed()

    assert [item.entry_id for item in waiting] == [entry_id]
    assert waiting[0].text == "Надо посмотреть фильм."


async def test_reply_to_the_owner_shows_the_clean_version(
    voice_entry_with_transcript: MakeEntry,
) -> None:
    """Сообщение о готовой разметке — тоже показ, хотя собирается в `enrich`.

    Отдельным тестом, потому что это единственное `display_text()` в файле,
    где остальные четыре места — рабочие. Перепутать здесь легче всего.
    """
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )

    rows = await enrich_repository.rows_for_reply([entry_id])

    assert [row.text for row in rows] == ["Надо посмотреть фильм."]


async def test_annotation_batch_gets_the_verbatim_version(
    voice_entry_with_transcript: MakeEntry,
) -> None:
    """Разметка получает дословный текст: над ним она считает границы единиц.

    Возьми она вычищенный — отрезки единиц оказались бы посчитаны над одним
    текстом, а нарезаны из другого, и дела показали бы чужие куски.
    """
    await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )

    batch = await enrich_repository.entries_needing_annotation(limit=1)

    assert [item.text for item in batch] == ["э ну надо мне посмотреть фильм"]


async def test_indexing_gets_the_verbatim_version(voice_entry_with_transcript: MakeEntry) -> None:
    """В чанки и эмбеддинги уходит дословный: поиск стоит на том же тексте.

    Это вторая половина обещания ADR-0015: потерянное чисткой остаётся
    находимым. Уйди в индекс вычищенный — оно исчезло бы молча.
    """
    entry_id = await voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )

    found = await memory_repository.entry_text(entry_id)

    assert found == ("э ну надо мне посмотреть фильм", False)
