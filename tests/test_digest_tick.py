"""Тик касаний целиком — от расписания до кнопки и обратно.

**Главный тест здесь сквозной**, и он написан именно так по выводу 16 августа:
дыра с напоминаниями проехала дважды потому, что каждый кусок был проверен
отдельно, а цепочка целиком — нет. Здесь цепочка такая:

    тик → сводка ушла → владелец нажал «сделал» настоящей кнопкой
        → дело закрыто → назавтра его в сводке нет

Кнопка нажимается через настоящий `Dispatcher` с настоящим whitelist —
не вызовом обработчика напрямую. Иначе проверялись бы наши представления
о том, как aiogram разбирает `callback_data`.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram import digest_keyboards
from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core import ingest
from brain.core.digest import loops, quiet, service, touches
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.touch import SilenceReason, TouchSlot
from tests.fakes import FakeNotifier
from tests.telegram_harness import CHAT_ID, FakeSession, make_bot, make_callback_update

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"

# 09:30 по Риму 20 августа 2026 — внутри утреннего окна.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
# 22:00 по Риму — уже вне всех окон, кроме вечернего.
NIGHT = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)

TASKS = "купить хлеб. позвонить маме."


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="123:test",
        telegram_allowed_user_ids=[CHAT_ID],
        google_api_key="key",
        brain_timezone=TIMEZONE,
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


@pytest.fixture
def dispatcher(settings: Settings) -> Dispatcher:
    """Настоящий диспетчер с настоящим whitelist — тот же, что в проде."""
    return build_dispatcher(settings)


async def _entry_with_tasks(text: str = TASKS, message_id: int = 10) -> int:
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=message_id + 1,
                text=text,
            ),
            is_private=False,
            timezone=TIMEZONE,
            now=MORNING - timedelta(days=1),
        )
    entry_id = result.entry_id
    assert entry_id is not None

    split = text.index("позвонить")
    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.TASK,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[
            MergedUnit(
                start=0,
                end=split,
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            ),
            MergedUnit(
                start=split,
                end=len(text),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            ),
        ],
        transcript_id=None,
        text=text,
    )
    return entry_id


async def test_empty_archive_stays_silent_and_says_why() -> None:
    """Молчание — обычный исход, и оно записано.

    Это основной режим Фазы 4 на молодом архиве, а не редкий угол: в живом
    архиве на 19 августа не было ни одного незакрытого дела, петля была одна,
    и всплывать было нечему.
    """
    notifier = FakeNotifier()

    sent = await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    assert sent == []
    assert notifier.mornings == []
    recorded = await touches.of_day(MORNING.date())
    assert recorded[TouchSlot.MORNING].silence_reason is SilenceReason.NOTHING_TO_SAY


async def test_morning_summary_goes_out_and_is_written_down() -> None:
    await _entry_with_tasks()
    notifier = FakeNotifier()

    sent = await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    assert sent == [TouchSlot.MORNING]
    assert len(notifier.mornings) == 1
    chat, text, commitment_ids, _ = notifier.mornings[0]
    assert chat == CHAT_ID
    assert "купить хлеб" in text
    # Два дела, а не одно: соседние единицы одного типа не схлопываются
    # (ADR-0012), и сводка считает дела поштучно.
    assert len(commitment_ids) == 2


async def test_a_second_tick_in_the_same_day_sends_nothing() -> None:
    """Тик просыпается каждые пять минут, а сводка бывает одна."""
    await _entry_with_tasks()
    notifier = FakeNotifier()

    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)
    await service.tick(
        now=MORNING + timedelta(minutes=5), timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier
    )

    assert len(notifier.mornings) == 1


async def test_pause_silences_the_summary_but_records_why() -> None:
    await _entry_with_tasks()
    await quiet.pause(MORNING - timedelta(hours=1))
    notifier = FakeNotifier()

    sent = await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    assert sent == []
    assert notifier.mornings == []
    recorded = await touches.of_day(MORNING.date())
    assert recorded[TouchSlot.MORNING].silence_reason is SilenceReason.QUIET


async def test_loop_is_marked_asked_only_after_the_summary_leaves() -> None:
    """Промежуток в две недели считается от настоящего вопроса.

    Отметить «спросили» до отправки значило бы, что упавшая отправка украла
    у петли четырнадцать дней молча.
    """
    entry_id = await _entry_with_tasks()
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше")
    notifier = FakeNotifier()

    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    asked = await loops.by_id(loop_id)
    assert asked is not None
    assert asked.last_asked_at is not None


async def test_thrice_snoozed_loop_changes_the_question() -> None:
    """Девятая дыра того же почерка, найдена 24 августа 2026.

    `loops.is_stuck` была написана, покрыта тестом и не звалась из `src/`
    ни разу: `_prepare` не клал признак в нагрузку, `_send` не передавал его
    отправителю, и смена вопроса после третьего «ещё думаю» (`vision.md` §5.2)
    не работала с написания Фазы 4. Поэтому тест идёт от тика, а не от чистой
    функции: чистая функция всё это время отвечала правильно, а до владельца
    ответ не доезжал.

    Откладывания копятся так же, как в проде — спросили и отложили, три круга
    по четырнадцать дней. Выставить `times_asked` руками значило бы проверить
    нашу догадку о колонке вместо пути через петлю.
    """
    entry_id = await _entry_with_tasks()
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше")

    moment = MORNING - loops.INTERVAL * 4
    for _ in range(loops.SNOOZES_BEFORE_DECISION):
        await loops.mark_asked(loop_id, now=moment)
        await loops.still_thinking(loop_id, now=moment)
        moment += loops.INTERVAL

    notifier = FakeNotifier()
    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    _, _, _, asked_loop = notifier.mornings[0]
    assert asked_loop == loop_id
    assert notifier.stuck_loops == [loop_id]


async def test_thrice_snoozed_loop_changes_the_question_in_the_text_too() -> None:
    """Кнопки уже спрашивали «Закрыть · Переформулирую», а текст — «Сдвинулось?».

    Та же дыра, что и у признака в нагрузке, только с другой стороны: до текста
    он не доезжал вовсе. Проверяется от тика, а не от рендера, потому что рендер
    и раньше отвечал бы правильно — не доходило до него.
    """
    entry_id = await _entry_with_tasks()
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше")

    moment = MORNING - loops.INTERVAL * 4
    for _ in range(loops.SNOOZES_BEFORE_DECISION):
        await loops.mark_asked(loop_id, now=moment)
        await loops.still_thinking(loop_id, now=moment)
        moment += loops.INTERVAL

    notifier = FakeNotifier()
    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    _, text, _, _ = notifier.mornings[0]
    assert "Закрыть или переформулируешь?" in text
    assert "Сдвинулось?" not in text


async def test_closing_a_deed_from_the_summary_removes_it_tomorrow(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Сквозной тест фазы: тик → кнопка → закрыто → назавтра нет в сводке.

    Кнопка нажимается настоящим апдейтом через настоящий диспетчер: дыра №3
    Фазы 1 проехала дважды именно потому, что куски проверялись поодиночке.
    """
    await _entry_with_tasks()
    notifier = FakeNotifier()

    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)
    _, first_text, commitment_ids, _ = notifier.mornings[0]
    assert "купить хлеб" in first_text

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            digest_keyboards.deed_data(digest_keyboards.DONE, commitment_ids[0]),
            message_id=500,
        ),
    )

    # Ответ — всплывающей подсказкой, а не правкой сводки: в ней несколько дел,
    # и стирать весь список из-за одного нажатия нельзя.
    assert session.calls_named("AnswerCallbackQuery")

    tomorrow = MORNING + timedelta(days=1)
    await service.tick(now=tomorrow, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    assert len(notifier.mornings) == 2
    _, second_text, remaining, _ = notifier.mornings[1]
    assert "купить хлеб" not in second_text
    assert "позвонить маме" in second_text
    assert len(remaining) == 1


async def test_stranger_cannot_close_someone_elses_deed(dispatcher: Dispatcher, bot: Bot) -> None:
    """Whitelist висит и на нажатиях — иначе чужой закрывал бы чужие дела."""
    await _entry_with_tasks()
    notifier = FakeNotifier()
    await service.tick(now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)
    _, _, commitment_ids, _ = notifier.mornings[0]

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            digest_keyboards.deed_data(digest_keyboards.DONE, commitment_ids[0]),
            message_id=500,
            user_id=999_999,
        ),
    )

    tomorrow = MORNING + timedelta(days=1)
    await service.tick(now=tomorrow, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)
    _, _, still_open, _ = notifier.mornings[1]
    assert len(still_open) == 2


async def test_evening_is_silent_without_disputed_entries() -> None:
    """Вечер приходит только с отдачей (`vision.md` §7.0).

    Уверенная разметка вечер не поднимает — и это не «нечего показать»,
    а сознательный отказ от сообщения «ты чего-то не разобрал».
    """
    await _entry_with_tasks()
    notifier = FakeNotifier()

    await service.tick(now=NIGHT, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier)

    assert notifier.evenings == []
    recorded = await touches.of_day(NIGHT.date())
    assert recorded[TouchSlot.EVENING].silence_reason is SilenceReason.NOTHING_TO_SAY
