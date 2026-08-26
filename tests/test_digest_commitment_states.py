"""Четыре состояния дела и кнопки под сводкой — от нажатия до следующей сводки.

Тесты здесь **путевые**, и это не оформление. Функция перехода была бы зелёной
и в том случае, если бы кнопка звала её без срока: дело ушло бы в `snoozed`
навсегда, а заметить это было бы неоткуда — оно просто перестало бы появляться.
Поэтому цепочка проверяется целиком:

    сводка ушла → владелец нажал настоящую кнопку → строка в базе
        → следующая сводка показывает (или не показывает) дело

Клавиатура между нажатиями живёт так же, как в Telegram: ответ обработчика
становится тем, что придёт с очередным нажатием. Иначе три уровня кнопок
проверялись бы по одному, а разворачивается-то один ряд из трёх.

Данные — живые: запись 51 архива и её настоящие отрезки, по которым в рабочей
базе заведены три дела. Придуманная строка не ломает нумерацию и обрезку так,
как ломает своя же речь.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup

from brain.adapters.telegram.bot import build_dispatcher
from brain.adapters.telegram.notifier import TelegramNotifier
from brain.config import Settings
from brain.core import ingest
from brain.core.digest import commitments, loops, morning, render
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.commitment import CommitmentState
from brain.storage.models.loop import LoopStatus
from tests import archive as archive_module
from tests.telegram_harness import CHAT_ID, FakeSession, make_bot, make_callback_update

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
ZONE = ZoneInfo(TIMEZONE)

# Запись 51 живого архива: одно голосовое, в котором разметка нашла три задачи.
# Отрезки — те самые, по которым дела заведены в рабочей базе.
ENTRY = 51
SPANS = ((242, 303), (303, 473), (473, 753))

# Подписи утверждены владельцем. Правило: подпись говорит, **что произойдёт**,
# а не спрашивает и не отвечает за владельца в прошедшем времени.
CHOICE = ["Завершить", "Взять в работу", "Отложить", "Убрать"]
TERMS = ["До завтра", "На неделю", "На месяц"]
BACK = "Назад"
RETURN = "Вернуть"

# Петля: у мысли «Закрыть» и «Убрать» звучат одинаково, поэтому глаголы другие.
LOOP_CHOICE = ["Решено", "Отложить", "Отпало"]
LOOP_STUCK_CHOICE = ["Решено", "Изменить формулировку", "Отпало"]


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


@pytest.fixture
def speech(archive: list[archive_module.Entry]) -> str:
    return archive_module.entry(archive, ENTRY).text


async def _three_deeds(text: str) -> None:
    """Три дела так, как их заводит разметка: из единиц одной записи."""
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(chat_id=CHAT_ID, message_id=10, ack_message_id=11, text=text),
            is_private=False,
            timezone=TIMEZONE,
            now=datetime.now(UTC) - timedelta(days=1),
        )
    entry_id = result.entry_id
    assert entry_id is not None

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
                start=start,
                end=end,
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
            for start, end in SPANS
        ],
        transcript_id=None,
        text=text,
    )


class Conversation:
    """Сводка в переписке: клавиатура переживает нажатия, как в Telegram.

    Нажатие приходит вместе с тем сообщением, под которым нажали, — включая
    его кнопки. Обработчик перерисовывает ряд одного дела, и следующее нажатие
    обязано прийти уже с перерисованной клавиатурой, иначе второй и третий
    уровень проверялись бы в вакууме.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        bot: Bot,
        session: FakeSession,
        *,
        message_id: int,
        markup: InlineKeyboardMarkup | None,
    ) -> None:
        self._dispatcher = dispatcher
        self._bot = bot
        self._session = session
        self._message_id = message_id
        self.markup = markup

    async def press(self, data: str) -> None:
        await self._dispatcher.feed_update(
            self._bot,
            make_callback_update(data, message_id=self._message_id, reply_markup=self.markup),
        )
        drawn = _last_markup(self._session)
        if drawn is not None:
            self.markup = drawn

    async def press_labelled(self, label: str) -> None:
        """Нажать кнопку по подписи — так, как её нажимает владелец."""
        await self.press(self.data(label))

    @property
    def rows(self) -> list[list[str]]:
        if self.markup is None:  # pragma: no cover — сводка без кнопок
            return []
        return [[button.text for button in row] for row in self.markup.inline_keyboard]

    def data(self, label: str) -> str:
        for row in self.markup.inline_keyboard if self.markup else []:
            for button in row:
                if button.text == label:
                    return button.callback_data or ""
        raise AssertionError(f"кнопки «{label}» нет: {self.rows}")


def _last_markup(session: FakeSession) -> InlineKeyboardMarkup | None:
    for call in reversed(session.calls):
        markup = getattr(call.method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            return markup
    return None


async def _summary(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, *, now: datetime, loop_id: int | None
) -> tuple[str, Conversation, list[int]]:
    """Отправить сводку так, как её отправляет воркер, и вернуть переписку."""
    content = await morning.collect(now)
    text = render.morning(content)
    ids = [item.id for item in content.commitments]
    message_id = await TelegramNotifier(bot).send_morning(
        CHAT_ID, text, commitment_ids=ids, loop_id=loop_id, loop_stuck=content.loop_stuck
    )
    return (
        text,
        Conversation(dispatcher, bot, session, message_id=message_id, markup=_last_markup(session)),
        ids,
    )


# --- Первый уровень: по одной кнопке на дело ---


async def test_summary_shows_one_button_per_deed(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Выбор не развёрнут: четыре кнопки на дело — это двенадцать под сводкой."""
    await _three_deeds(speech)
    _, chat, ids = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    assert len(ids) == 3
    assert chat.rows == [["1"], ["2"], ["3"]]


async def test_pressing_a_number_opens_the_choice_for_that_deed_only(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Развернулось второе дело — строки первого и третьего не изменились."""
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("2")

    assert chat.rows[0] == ["1"]
    assert chat.rows[-1] == ["3"]
    assert CHOICE in chat.rows
    assert [BACK] in chat.rows


async def test_back_folds_the_choice_away(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Промах по кнопке без «Назад» — тупик: развернувшееся не свернуть."""
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("2")
    await chat.press_labelled(BACK)

    assert chat.rows == [["1"], ["2"], ["3"]]


# --- Второй уровень: что произойдёт с делом ---


async def test_done_deed_leaves_the_next_summary(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Завершить")

    stored = await commitments.by_id(ids[0])
    assert stored is not None and stored.state is CommitmentState.DONE
    assert ids[0] not in [item.id for item in (await morning.collect(now)).commitments]


async def test_in_progress_stays_in_the_summary_with_an_hourglass(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """«В работе» — не награда за начало: дело остаётся на месте, помеченным.

    Поднять его наверх значило бы завести счётчик усердия, а `vision.md` §7.0
    запрещает счётчики именно в касаниях.
    """
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("2")
    await chat.press_labelled("Взять в работу")

    stored = await commitments.by_id(ids[1])
    assert stored is not None and stored.state is CommitmentState.IN_PROGRESS

    content = await morning.collect(now)
    assert [item.id for item in content.commitments] == ids
    marked = [line for line in render.morning(content).splitlines() if line.startswith("2.")]
    assert marked and marked[0].endswith("⏳")


async def test_a_deed_in_work_can_be_finished_in_the_same_message(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """«В работе» — единственный переход, после которого выбор остаётся открытым.

    `done`, `snoozed` и `dropped` убирают дело из сводки: действовать после них
    больше не над чем, и подтверждение с отменой там на своём месте.
    А `in_progress` **оставляет дело на месте** — пометил «в работе», через час
    сделал, и завершить нечем до завтрашней сводки. Отмена здесь и не нужна:
    дело видно, состояние видно, менять можно в любой момент.
    """
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Взять в работу")

    assert CHOICE in chat.rows, "выбор обязан остаться развёрнутым"
    assert RETURN not in [label for row in chat.rows for label in row]

    await chat.press_labelled("Завершить")

    stored = await commitments.by_id(ids[0])
    assert stored is not None and stored.state is CommitmentState.DONE


async def test_back_still_folds_a_deed_that_is_in_work(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Развёрнутый ряд «в работе» живёт дольше остальных — и сворачивается так же.

    Номер при сворачивании восстанавливается из порядка рядов, а этот ряд
    занимает два: если бы порядок считался по строкам, а не по делам, второе
    дело вернулось бы под чужим номером.
    """
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("2")
    await chat.press_labelled("Взять в работу")
    await chat.press_labelled(BACK)

    assert chat.rows == [["1"], ["2"], ["3"]]


async def test_dropped_deed_leaves_the_next_summary(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("3")
    await chat.press_labelled("Убрать")

    stored = await commitments.by_id(ids[2])
    assert stored is not None and stored.state is CommitmentState.DROPPED


# --- Третий уровень: срок откладывания ---


async def test_snooze_offers_three_terms(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Отложить")

    assert TERMS in chat.rows
    assert [BACK] in chat.rows


async def test_snoozing_stores_the_term_together_with_the_state(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Главный тест задачи: `snoozed` без срока — вечно скрытое дело.

    Заметить такое было бы неоткуда: дело просто перестало бы приходить.
    Поэтому путь идёт от нажатия до строки в базе, а не от функции перехода —
    функция и с пустым сроком отвечала бы «готово».
    """
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Отложить")
    await chat.press_labelled("На неделю")

    stored = await commitments.by_id(ids[0])
    assert stored is not None
    assert stored.state is CommitmentState.SNOOZED
    assert stored.snoozed_until is not None
    assert now + timedelta(days=6) < stored.snoozed_until < now + timedelta(days=8)


async def test_snoozed_deed_leaves_the_summary_until_its_term(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Отложить")
    await chat.press_labelled("На неделю")

    assert ids[0] not in [item.id for item in (await morning.collect(now)).commitments]
    later = now + timedelta(days=8)
    assert ids[0] in [item.id for item in (await morning.collect(later)).commitments]


async def test_until_tomorrow_means_tomorrows_summary_not_plus_a_day(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """«До завтра», отсчитанное сутками от нажатия, вернуло бы дело послезавтра.

    Отложили в одиннадцать утра — сутки кончаются в одиннадцать следующего дня,
    а сводка приходит в девять. Срок поэтому считается по календарю владельца,
    а не по часам.
    """
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Отложить")
    await chat.press_labelled("До завтра")

    tomorrow_morning = (now.astimezone(ZONE) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    assert ids[0] in [item.id for item in (await morning.collect(tomorrow_morning)).commitments], (
        "дело обязано вернуться в завтрашнюю сводку, а не через сутки после нажатия"
    )


# --- Отмена последнего действия ---


async def test_confirmation_offers_a_way_back(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Завершить")

    assert ["Завершено", RETURN] in chat.rows


async def test_return_restores_the_previous_state_not_always_open(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Дело было «в работе» до того, как его завершили, — туда и возвращается.

    Прежнее состояние едет в `callback_data` кнопки возврата: гадать по данным
    нечем, а вернуть «в работе» в `open` значило бы молча стереть то, что
    владелец про это дело уже сказал.

    Путь настоящий: сегодня взял в работу, завтра в новой сводке завершил
    и промахнулся. Внутри одного сообщения такого не бывает — строка дела
    после нажатия становится подтверждением, и второй раз по ней не нажать.
    """
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, today, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await today.press_labelled("1")
    await today.press_labelled("Взять в работу")

    _, tomorrow, _ = await _summary(
        dispatcher, bot, session, now=now + timedelta(days=1), loop_id=None
    )
    await tomorrow.press_labelled("1")
    await tomorrow.press_labelled("Завершить")
    await tomorrow.press_labelled(RETURN)

    stored = await commitments.by_id(ids[0])
    assert stored is not None
    assert stored.state is CommitmentState.IN_PROGRESS


async def test_return_from_snoozed_clears_the_term(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """Иначе дело вернулось бы в список со сроком, которого уже нет."""
    await _three_deeds(speech)
    now = datetime.now(UTC)
    _, chat, ids = await _summary(dispatcher, bot, session, now=now, loop_id=None)

    await chat.press_labelled("1")
    await chat.press_labelled("Отложить")
    await chat.press_labelled("На месяц")
    await chat.press_labelled(RETURN)

    stored = await commitments.by_id(ids[0])
    assert stored is not None
    assert stored.state is CommitmentState.OPEN
    assert stored.snoozed_until is None
    assert ids[0] in [item.id for item in (await morning.collect(now)).commitments]


async def test_returning_folds_the_row_back_to_its_number(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """После отмены дело снова обычное — и ряд у него обычный."""
    await _three_deeds(speech)
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=None)

    await chat.press_labelled("2")
    await chat.press_labelled("Убрать")
    await chat.press_labelled(RETURN)

    assert chat.rows == [["1"], ["2"], ["3"]]


# --- Петля: те же нажатия, другие глаголы ---


async def _loop(question: str) -> int:
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(chat_id=CHAT_ID, message_id=20, ack_message_id=21, text=question),
            is_private=False,
            timezone=TIMEZONE,
            now=datetime.now(UTC) - timedelta(days=1),
        )
    assert result.entry_id is not None
    return await loops.create(entry_id=result.entry_id, unit_id=None, question=question)


async def test_loop_buttons_say_what_happens(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """«Закрыть» и «Убрать» у мысли звучат одинаково — и жались бы наугад."""
    loop_id = await _loop(speech[:200])
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=loop_id)

    assert LOOP_CHOICE in chat.rows


async def test_loop_snoozed_thrice_offers_to_rephrase(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """После третьего откладывания «Отложить» не предлагается вовсе."""
    loop_id = await _loop(speech[:200])
    moment = datetime.now(UTC) - loops.INTERVAL * 4
    for _ in range(loops.SNOOZES_BEFORE_DECISION):
        await loops.mark_asked(loop_id, now=moment)
        await loops.still_thinking(loop_id, now=moment)
        moment += loops.INTERVAL

    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=loop_id)

    assert LOOP_STUCK_CHOICE in chat.rows


async def test_loop_solved_leaves_the_next_summary(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    loop_id = await _loop(speech[:200])
    now = datetime.now(UTC)
    _, chat, _ = await _summary(dispatcher, bot, session, now=now, loop_id=loop_id)

    await chat.press_labelled("Решено")

    closed = await loops.by_id(loop_id)
    assert closed is not None and closed.status is LoopStatus.CLOSED
    assert (await morning.collect(now)).loop is None


async def test_loop_snoozed_from_a_button_waits_two_weeks(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """«Отложить» — тот же переход, что был у «Ещё думаю»: сдвиг на две недели."""
    loop_id = await _loop(speech[:200])
    now = datetime.now(UTC)
    _, chat, _ = await _summary(dispatcher, bot, session, now=now, loop_id=loop_id)

    await chat.press_labelled("Отложить")

    snoozed = await loops.by_id(loop_id)
    assert snoozed is not None
    assert snoozed.times_asked == 1
    assert snoozed.snooze_until is not None
    assert (await morning.collect(now)).loop is None


async def test_loop_gone_is_not_the_same_as_solved(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, speech: str
) -> None:
    """`dropped` у петли сохраняется намеренно: выбросить состояние можно
    когда угодно, а вернуть — только вместе с данными, которых уже не будет."""
    loop_id = await _loop(speech[:200])
    _, chat, _ = await _summary(dispatcher, bot, session, now=datetime.now(UTC), loop_id=loop_id)

    await chat.press_labelled("Отпало")

    gone = await loops.by_id(loop_id)
    assert gone is not None and gone.status is LoopStatus.IRRELEVANT
