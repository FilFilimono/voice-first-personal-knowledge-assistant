"""Привычка от слова до напоминания — путь целиком, а не по функциям.

Правило проекта: **тест на функцию не заменяет теста на путь**, и цена ему
`loops.is_stuck` — написана, покрыта тестом, не звалась из `src/` ни разу,
и смена вопроса у петли не работала месяц. Привычка устроена так же опасно:
модель, ритм, кнопка, тик и рендер написаны по отдельности, и каждый кусок
можно проверить зелёным, не соединив ни один с соседним.

Путь, который проходит этот файл, — ровно тот, что записан в плане Фазы 4:

    сказать «каждый день читать» → разметка ставит тип habit
    → под разбором появляется кнопка → нажатие заводит привычку
    → назавтра приходит напоминание
    → **дело при этом не заводится и в утренней сводке не появляется**

Последняя строка не довесок: `vision.md` §5.11 говорит, что привычка не висит
делом **по устройству**, а не по показу. Проверяется это на записи, где рядом
с привычкой стоит настоящая задача, — иначе «дел нет» доказывало бы только то,
что дел нет вовсе.

Ни один вызов модели здесь не настоящий: разметка отвечает по сценарию,
эмбеддер — заглушка. Живой квоты у дорогой модели двадцать запросов в сутки.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from brain.adapters.telegram.bot import build_dispatcher
from brain.adapters.telegram.notifier import TelegramNotifier
from brain.config import Settings
from brain.core import ingest
from brain.core.digest import quiet, service
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.scheduler import jobs
from brain.storage.db import session_scope
from brain.storage.models.commitment import Commitment
from brain.storage.models.habit import Habit, Rhythm
from tests import archive as archive_module
from tests.fakes import FakeNotifier, ScriptedModel, StubEmbedder, answer_units
from tests.telegram_harness import CHAT_ID, OWNER_ID, FakeSession, make_bot, make_callback_update

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
ACK_MESSAGE_ID = 941

# Живая запись: «Книгу … И начну с этого дня каждый день её читать. Минимум 10
# страниц», а следом «Нужно ещё find починить». Привычка и задача в одной
# записи — так и говорят вслух, и так проверяется, что дело заводится
# из задачи и **не** заводится из привычки.
HABIT_ENTRY = 53
HABIT_STARTS = "Книгу у меня"
TASK_STARTS = "Нужно ещё find"

# 09:30 по Риму назавтра после заведения — привычка назначена на девять.
TOMORROW_MORNING = datetime(2026, 8, 25, 7, 30, tzinfo=UTC)
YESTERDAY = TOMORROW_MORNING - timedelta(days=1)


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


@pytest.fixture
def dispatcher(migrated_database: str) -> Dispatcher:
    return build_dispatcher(
        Settings(
            _env_file=None,
            telegram_bot_token="123:test-token",
            google_api_key="AI-test",
            telegram_allowed_user_ids=[OWNER_ID],
            brain_timezone=TIMEZONE,
        )
    )


async def _capture(body: str) -> int:
    """Приём — настоящим `ingest`, а не вставкой строки.

    Момент — «сейчас», потому что разбор дописывается к подтверждению приёма
    только пока оно свежее (`jobs.ACK_WINDOW`), а кнопка привычки живёт именно
    под ним.
    """
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID, message_id=10, ack_message_id=ACK_MESSAGE_ID, text=body
            ),
            is_private=False,
            timezone=TIMEZONE,
            now=datetime.now(UTC),
        )
    assert result.entry_id is not None
    return result.entry_id


async def _annotate(entry_id: int, body: str, notifier: TelegramNotifier) -> None:
    """Разметка через рабочее задание воркера, а не через сервис напрямую.

    Кнопка появляется в `jobs.enrich` → `notifier.show_annotation`, и позвать
    сервис разметки в обход значило бы проверить всё, кроме того места, где
    предложение вообще показывается.

    Цитаты начала берутся **из живого текста**: придуманная цитата найдётся
    не там или не найдётся вовсе.
    """
    model = ScriptedModel(
        [
            answer_units(
                entry_id,
                ("Вот это по боту", "thought"),
                (HABIT_STARTS, "habit"),
                (TASK_STARTS, "task"),
            )
        ]
    )
    assert HABIT_STARTS in body and TASK_STARTS in body

    await jobs.enrich(
        entry_id=entry_id,
        entry_ids=None,
        model=model,
        titler=model,
        embedder=StubEmbedder(),
        notifier=notifier,
        limit=25,
    )


async def _habits() -> list[Habit]:
    async with session_scope() as db:
        return list((await db.scalars(select(Habit))).all())


async def _commitments(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(select(Commitment.text))
            return [str(row[0]) for row in rows]
    finally:
        await engine.dispose()


async def test_a_habit_goes_from_a_spoken_phrase_to_a_daily_reminder(
    migrated_database: str,
    archive: list[archive_module.Entry],
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
) -> None:
    """Путь целиком: сказал → предложили → нажал → назавтра спросили.

    Утверждений четыре, и каждое проверяется в том месте, где оно ломается:
    кнопка — в исходящем сообщении, заведение — строкой в базе, ритм — тем,
    что его никто не спрашивал (в записи он назван словами), напоминание —
    беззвучным сообщением из тика.
    """
    body = archive_module.entry(archive, HABIT_ENTRY).text
    entry_id = await _capture(body)

    await _annotate(entry_id, body, TelegramNotifier(bot))

    # 1. Разметка нашла привычку — и система **предложила**, а не завела.
    assert (f"habit:new:{entry_id}") in dict(session.last_buttons()).values()
    assert await _habits() == []

    # 2. Нажатие. Ритм назван в самой записи, поэтому спрашивать нечего.
    await dispatcher.feed_update(
        bot, make_callback_update(f"habit:new:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    created = await _habits()
    assert len(created) == 1
    assert created[0].rhythm is Rhythm.DAILY
    assert created[0].source_entry_id == entry_id
    assert "каждый день" in created[0].text

    # 3. Назавтра приходит напоминание — беззвучно и вне слотов.
    notifier = FakeNotifier()
    await service.tick(
        now=TOMORROW_MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    assert len(notifier.quiet) == 1
    _, reminder = notifier.quiet[0]
    assert "каждый день" in reminder.lower()
    assert reminder.endswith("Как продвижение? Что получается, что мешает?")

    # 4. Дело из привычки не завелось, а из соседней задачи — завелось.
    #    Пустой список дел доказывал бы только, что разметка ничего не нашла.
    deeds = await _commitments(migrated_database)
    assert len(deeds) == 1
    assert deeds[0].startswith(TASK_STARTS)


async def test_the_pause_silences_the_summary_but_not_the_habit(
    migrated_database: str,
    archive: list[archive_module.Entry],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    """`/pause` глушит инициативные касания и сводку, но не заказанное.

    `vision.md` §7.1: «продолжают приходить только напоминания, которые я
    поставил сам». Привычка — ровно это: владелец нажал кнопку. Сломать легко
    и незаметно — достаточно позвать её после проверки паузы, и она станет
    инициативной, ничем этого не показав.
    """
    body = archive_module.entry(archive, HABIT_ENTRY).text
    entry_id = await _capture(body)
    await _annotate(entry_id, body, TelegramNotifier(bot))
    await dispatcher.feed_update(
        bot, make_callback_update(f"habit:new:{entry_id}", message_id=ACK_MESSAGE_ID)
    )
    await quiet.pause(YESTERDAY)

    notifier = FakeNotifier()
    await service.tick(
        now=TOMORROW_MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    assert notifier.mornings == []
    assert len(notifier.quiet) == 1


async def test_the_habit_never_shows_up_in_the_morning_summary(
    migrated_database: str,
    archive: list[archive_module.Entry],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    """Привычка не висит делом — и это видно в тексте, а не только в таблице.

    Проверяется текстом сводки, потому что сломать это можно двумя разными
    способами: завести дело из единицы `habit` и показать привычку в списке
    висящего. Второе не поймает ни один запрос к `commitments`.
    """
    body = archive_module.entry(archive, HABIT_ENTRY).text
    entry_id = await _capture(body)
    await _annotate(entry_id, body, TelegramNotifier(bot))
    await dispatcher.feed_update(
        bot, make_callback_update(f"habit:new:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    notifier = FakeNotifier()
    await service.tick(
        now=TOMORROW_MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    assert len(notifier.mornings) == 1
    _, summary, commitment_ids, _ = notifier.mornings[0]
    assert len(commitment_ids) == 1
    assert HABIT_STARTS not in summary
    assert "каждый день" not in summary.lower()


async def test_a_habit_without_a_rhythm_is_asked_and_not_guessed(
    migrated_database: str,
    archive: list[archive_module.Entry],
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
) -> None:
    """Ритма в словах нет — бот спрашивает, а не угадывает (`vision.md` §5.11).

    Умолчания здесь нет намеренно: угаданный ритм это ежедневное сообщение,
    выбранное за владельца, и ошибка в нём обнаружится не разговором,
    а раздражением. Поэтому нажатие «Привычка» на записи без ритма обязано
    не завести ничего, а показать четыре кнопки из §5.11.
    """
    body = "хочу читать книгу перед сном, хотя бы понемногу"
    entry_id = await _capture(body)
    await _annotate_whole(entry_id, TelegramNotifier(bot))

    await dispatcher.feed_update(
        bot, make_callback_update(f"habit:new:{entry_id}", message_id=ACK_MESSAGE_ID)
    )

    assert await _habits() == []
    labels = [label for label, _ in session.last_buttons()]
    assert labels == ["Каждый день", "Через день", "По будням", "Свой вариант"]

    await dispatcher.feed_update(
        bot, make_callback_update(f"habit:{Rhythm.WEEKDAYS.value}:{entry_id}", message_id=942)
    )

    created = await _habits()
    assert len(created) == 1
    assert created[0].rhythm is Rhythm.WEEKDAYS
    assert created[0].days == [0, 1, 2, 3, 4]


async def _annotate_whole(entry_id: int, notifier: TelegramNotifier) -> None:
    """Разметка одной единицей типа `habit` на всю запись."""
    model = ScriptedModel([answer_units(entry_id, ("хочу читать", "habit"))])
    await jobs.enrich(
        entry_id=entry_id,
        entry_ids=None,
        model=model,
        titler=model,
        embedder=StubEmbedder(),
        notifier=notifier,
        limit=25,
    )
