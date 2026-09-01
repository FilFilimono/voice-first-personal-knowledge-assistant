"""Кнопки под готовым разбором: «не туда» и «сделать приватной».

Проверяется путь целиком через настоящий `Dispatcher` и настоящую базу — от
нажатия до строки в `entry_overrides`. Иначе проверялись бы наши представления
о том, как aiogram разбирает нажатие, а не сам разбор.

Что должно работать:

1. воркер дописывает разметку к подтверждению приёма и вешает две кнопки
2. старую запись он не трогает: править переписку задним числом никто не просил
3. «не туда» открывает список типов, а текст записи остаётся на месте
4. выбор типа пишет оверрайд и убирает кнопки
5. «сделать приватной» говорит вслух, что текст уже уходил наружу
6. чужие нажатия игнорируются — whitelist висит и на кнопках

Пункт 5 — условие, при котором ADR-0002 вообще разрешает ретроактивную
приватность. Молчаливое «готово» здесь означало бы обещание, которого система
выполнить не может.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import Select, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.scheduler import jobs
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.topic import Topic
from tests.fakes import FakeNotifier, ScriptedModel, StubEmbedder, answer
from tests.telegram_harness import (
    CHAT_ID,
    OWNER_ID,
    STRANGER_ID,
    FakeSession,
    make_bot,
    make_callback_update,
)

pytestmark = pytest.mark.integration

ACK_MESSAGE_ID = 901
BODY = "надо разобраться, почему поиск иногда молчит"


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


@pytest.fixture
def dispatcher(migrated_database: str) -> Dispatcher:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:test-token",
        google_api_key="AI-test",
        telegram_allowed_user_ids=[OWNER_ID],
    )
    return build_dispatcher(settings)


async def _entry(url: str, *, minutes_ago: int = 0) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = int(
                await connection.scalar(
                    insert(Entry)
                    .values(
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind.TEXT,
                        raw_text=BODY,
                        is_private=False,
                        timezone="Europe/Moscow",
                        dedup_key=f"buttons:{minutes_ago}",
                        meta={"chat_id": CHAT_ID, "ack_message_id": ACK_MESSAGE_ID},
                    )
                    .returning(Entry.id)
                )
                or 0
            )
            if minutes_ago:
                # `entries` не правится приложением, но это тестовая база
                # и разговор идёт напрямую с ней: другого способа получить
                # запись «недельной давности» нет.
                await connection.execute(
                    text("ALTER TABLE entries DISABLE TRIGGER USER"),
                )
                await connection.execute(
                    update(Entry)
                    .where(Entry.id == entry_id)
                    .values(created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))
                )
                await connection.execute(text("ALTER TABLE entries ENABLE TRIGGER USER"))
        return entry_id
    finally:
        await engine.dispose()


async def _annotate(entry_id: int, notifier: FakeNotifier) -> None:
    model = ScriptedModel([answer((entry_id, "loop", ["поиск по архиву"]))])
    await jobs.enrich(
        entry_id=entry_id,
        model=model,
        # Модель названий отдельная и здесь ничего не называет: разметка типа
        # `loop` дел не заводит, а петля появится только от нажатия кнопки.
        titler=ScriptedModel([{}]),
        embedder=StubEmbedder(),
        notifier=notifier,
        limit=25,
    )


async def _rows(url: str, statement: Select[Any]) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in await connection.execute(statement)]
    finally:
        await engine.dispose()


async def test_annotation_is_appended_to_the_acknowledgement(migrated_database: str) -> None:
    """Одна запись — одно уведомление: разбор дописывается, а не присылается."""
    entry_id = await _entry(migrated_database)
    notifier = FakeNotifier()

    await _annotate(entry_id, notifier)

    assert notifier.sent == [], "разбор не должен быть вторым сообщением"
    [(chat_id, message_id, shown, shown_entry)] = notifier.annotated
    assert (chat_id, message_id, shown_entry) == (CHAT_ID, ACK_MESSAGE_ID, entry_id)
    assert "📌 открытый вопрос" in shown
    assert "поиск по архиву" in shown


async def test_old_entry_is_annotated_silently(migrated_database: str) -> None:
    """Первый прогон разберёт весь накопленный архив — и это не повод
    переписывать сообщения недельной давности."""
    entry_id = await _entry(migrated_database, minutes_ago=60 * 24 * 7)
    notifier = FakeNotifier()

    await _annotate(entry_id, notifier)

    assert notifier.annotated == []
    # Разметка при этом сделана — молчит только бот.
    # Только темы, заведённые разметкой: базовые приходят миграцией и своей
    # записи-источника не имеют (ADR-0011, «Обновление»).
    assert await _rows(
        migrated_database, select(Topic.name).where(Topic.source_entry_id.is_not(None))
    ) == [("поиск по архиву",)]


async def test_wrong_button_opens_the_list_of_types(
    migrated_database: str, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    entry_id = await _entry(migrated_database)
    await _annotate(entry_id, FakeNotifier())

    await dispatcher.feed_update(
        bot, make_callback_update(f"mark:wrong:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    labels = [label for label, _ in session.last_buttons()]
    assert "мысль" in labels and "служебное" in labels
    # Считается по enum'у, а не числом: типов стало десять, когда появилась
    # привычка, и в этой строке стояла бы девятка, ничего не объясняя.
    # Утверждение здесь — «доступны все», а не «их столько-то».
    assert len(labels) == len(AnnotationType), "все типы должны быть доступны"
    assert "привычка" in labels


async def test_choosing_a_type_writes_an_override(
    migrated_database: str, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    entry_id = await _entry(migrated_database)
    await _annotate(entry_id, FakeNotifier())

    await dispatcher.feed_update(
        bot, make_callback_update(f"mark:set:insight:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    stored = await _rows(migrated_database, select(EntryOverride.action, EntryOverride.payload))
    assert stored == [(OverrideAction.RECLASSIFY, {"type": "insight", "was": "loop"})]
    assert "понимание" in session.texts[-1]
    assert "было: открытый вопрос" in session.texts[-1]
    assert session.last_buttons() == [], "после ответа кнопки должны исчезнуть"


async def test_private_button_admits_the_text_already_left(
    migrated_database: str, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """ADR-0002 разрешает эту кнопку ровно с этим условием."""
    entry_id = await _entry(migrated_database)
    await _annotate(entry_id, FakeNotifier())

    await dispatcher.feed_update(
        bot, make_callback_update(f"mark:private:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    shown = session.texts[-1]
    assert "приватная" in shown.lower()
    assert "уже уходил" in shown
    stored = await _rows(migrated_database, select(EntryOverride.action))
    assert stored == [(OverrideAction.MAKE_PRIVATE,)]


async def test_stranger_cannot_press_anything(
    migrated_database: str, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Whitelist висит и на нажатиях — иначе чужой правит чужой архив."""
    entry_id = await _entry(migrated_database)
    await _annotate(entry_id, FakeNotifier())

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            f"mark:private:{entry_id}", message_id=ACK_MESSAGE_ID, user_id=STRANGER_ID
        ),
    )

    assert await _rows(migrated_database, select(EntryOverride.id)) == []
