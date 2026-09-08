"""Событие в календаре — только по нажатию. Путь целиком, через `Dispatcher`.

Критерий готовности Фазы 5 звучит дословно: **«ни одно событие не создано без
моего нажатия»** (`tech_plan.md` §8), и проверяется он здесь **тестом пути,
а не намерением**: каждая проверка начинается с апдейта Telegram и кончается
тем, что увидел владелец и сколько раз позвался `events.create_for_entry`.

Три утверждения, и каждое ломается по-своему:

1. кнопка появляется **после подтверждения напоминания**, а не при приёме
   записи: пока владелец не сказал «да», времени у системы нет;
2. **создание зовётся ровно из нажатия и ниоткуда больше.** Это же проверено
   отдельно поиском по `src/` — см. `test_nothing_else_in_the_source_creates_events`;
3. владелец видит, **что именно** ушло: текст, время в его поясе и имя
   календаря. Молча события не создаются.

Плюс два отказа, которые обязаны выглядеть отказами, а не поломкой: календарь
не настроен и Google ответил ошибкой. Во втором случае первое, что читает
владелец, — «запись цела».

Настоящих часов этот файл не касается: время события приходит от заглушки
сервиса, а не считается здесь. Быстрый тест, база не нужна.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.integrations.gcal import events
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import (
    OWNER_ID,
    FakeSession,
    make_bot,
    make_callback_update,
    make_message,
    make_update,
)

# Два пути, на которых у записи появляется напоминание, и они разные.
# `DIRECT` — явное «напомни»: напоминание заводится сразу при приёме, вопроса
# нет. Это самый частый случай владельца и его собственная формулировка
# из замысла Фазы 5. `ASKED` — повелительный глагол без «напомни»: сначала
# кнопки «Да/Нет», напоминание только после подтверждения.
DIRECT = "напомни в четверг в 19:00 позвонить маме"
ASKED = "Помни выйти в 19:40."
SUBJECT = "позвонить маме"
ACK_MESSAGE_ID = 901

# Момент события в UTC. В Риме это 19:00 — и именно это владелец обязан
# прочитать: 17:00 было бы «правдой» в другом поясе.
WHEN = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)


class Calls:
    """Сколько раз система собралась наружу и с чем."""

    def __init__(self, result: events.Result) -> None:
        self.result = result
        self.entries: list[int] = []

    async def create(self, entry_id: int, *, now: datetime) -> events.Result:
        self.entries.append(entry_id)
        return self.result


class EntryStore:
    """Записи в памяти вместо базы — то же, что в `test_reminder_question_telegram`."""

    def __init__(self, uow: FakeUnitOfWork) -> None:
        self._uow = uow
        self.reminders_by_entry: set[int] = set()

    async def find_by_ack(self, chat_id: int, ack_message_id: int) -> tuple[int, str] | None:
        for number, draft in enumerate(self._uow.entries, start=1):
            meta = dict(draft.meta)
            if meta.get("chat_id") == chat_id and meta.get("ack_message_id") == ack_message_id:
                return number, draft.raw_text or ""
        return None

    async def has_reminder(self, entry_id: int) -> bool:
        return entry_id in self.reminders_by_entry


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def store(uow: FakeUnitOfWork) -> EntryStore:
    return EntryStore(uow)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Сервис записи заменён счётчиком.

    Подменяется **он**, а не клиент Google: этот файл про то, кто и сколько раз
    зовёт создание, а что оно делает внутри — предмет `test_calendar_privacy`,
    и там всё по-настоящему, на живой базе.
    """
    recorder = Calls(
        events.Result(
            outcome=events.Outcome.CREATED,
            title=SUBJECT,
            starts_at=WHEN,
            calendar="Brain",
        )
    )
    monkeypatch.setattr(events, "create_for_entry", recorder.create)
    return recorder


@pytest.fixture
def offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Календарь настроен, запись не приватная, события ещё нет."""

    async def yes(entry_id: int) -> bool:
        return True

    monkeypatch.setattr(events, "offered", yes)


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch, uow: FakeUnitOfWork, store: EntryStore
) -> Dispatcher:
    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield uow

    async def not_private() -> bool:
        return False

    async def no_prompting(message_id: int) -> None:
        return None

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.digest.prompting.answered", no_prompting)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", not_private)
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.entry_by_ack_message", store.find_by_ack
    )
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.repository.has_reminder", store.has_reminder
    )

    return build_dispatcher(
        Settings(
            _env_file=None,
            telegram_bot_token="123:test-token",
            google_api_key="AI-test",
            telegram_allowed_user_ids=[OWNER_ID],
        )
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


async def capture(dispatcher: Dispatcher, bot: Bot, text: str = DIRECT) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text=text, message_id=42)))


async def press(dispatcher: Dispatcher, bot: Bot, data: str) -> None:
    await dispatcher.feed_update(bot, make_callback_update(data, message_id=ACK_MESSAGE_ID))


def labels(session: FakeSession) -> list[str]:
    """Кнопки под **последним** сообщением, а не под последним, где они были.

    `FakeSession.last_buttons` отвечает на другой вопрос: она ищет назад
    до первой разметки и пропускает сообщения без кнопок. Для этого файла
    разница решающая — половина утверждений здесь звучит «кнопки больше нет»,
    и по `last_buttons` она была бы неотличима от «кнопка всё ещё есть».
    """
    markup = getattr(session.calls[-1].method, "reply_markup", None)
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def button(session: FakeSession, label: str) -> str:
    return next(data for text, data in session.last_buttons() if text == label)


# --- Кнопка появляется там, где ей место, и не раньше ---


async def test_capturing_a_direct_reminder_offers_the_calendar_and_creates_nothing(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, calls: Calls, offered: None
) -> None:
    """«Напомни в четверг в 19:00» — напоминание заведено, событие ещё нет.

    Здесь система уже знает и текст, и время, и соблазн завести всё разом
    наибольший. Кнопка при этом обязана быть: это тот самый случай, ради
    которого фаза и делалась, и путь через вопрос его не покрывает.
    """
    await capture(dispatcher, bot)

    assert labels(session) == ["🗓 В календарь"]
    assert calls.entries == [], "предложение — ещё не создание"


async def test_the_asked_path_offers_the_calendar_only_after_yes(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, calls: Calls, offered: None
) -> None:
    """Пока владелец не сказал «да», времени у системы нет — и кнопки тоже."""
    await capture(dispatcher, bot, ASKED)
    assert labels(session) == ["Да", "Нет"]
    assert calls.entries == []

    await press(dispatcher, bot, button(session, "Да"))

    assert labels(session) == ["🗓 В календарь"]
    assert calls.entries == []


async def test_refusing_the_reminder_leaves_no_calendar_button(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, calls: Calls, offered: None
) -> None:
    """«Нет» — значит нет: ни напоминания, ни предложения календаря."""
    await capture(dispatcher, bot, ASKED)
    await press(dispatcher, bot, button(session, "Нет"))

    assert calls.entries == []
    assert labels(session) == []


async def test_the_calendar_is_not_offered_when_events_say_no(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    calls: Calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не настроен, напоминания нет, приватная, уже создано — кнопки нет вовсе.

    Кнопка, которая ничего не делает, хуже отсутствующей: владелец нажимает,
    ничего не происходит, и узнать почему неоткуда.
    """

    async def no(entry_id: int) -> bool:
        return False

    monkeypatch.setattr(events, "offered", no)

    await capture(dispatcher, bot)

    assert labels(session) == []
    assert calls.entries == []


# --- Нажатие ---


async def test_the_press_creates_the_event_once_and_says_what_went(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, calls: Calls, offered: None
) -> None:
    """Владелец видит текст, время в своём поясе и имя календаря.

    Требование приёмки, а не вежливость: «молча события не создаются».
    """
    await capture(dispatcher, bot)
    await press(dispatcher, bot, button(session, "🗓 В календарь"))

    assert calls.entries == [1], "создание зовётся ровно один раз и ровно из нажатия"

    said = session.texts[-1]
    assert SUBJECT in said
    # 19:00 по Риму, а не 17:00 по UTC.
    assert "10.09 в 19:00" in said
    assert "Brain" in said
    # Кнопка снимается: второй раз она либо не сработает, либо даст второе
    # событие.
    assert labels(session) == []


async def test_a_refusal_from_google_says_the_entry_is_safe(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
    offered: None,
) -> None:
    """Отказ провайдера не роняет ничего — то же правило, что у косметики.

    Первое, что читает владелец, — что запись цела: отказ Google не имеет права
    выглядеть как потеря мысли.
    """
    recorder = Calls(
        events.Result(
            outcome=events.Outcome.FAILED,
            title=SUBJECT,
            starts_at=WHEN,
            detail="Refresh token отозван.",
        )
    )
    monkeypatch.setattr(events, "create_for_entry", recorder.create)

    await capture(dispatcher, bot)
    await press(dispatcher, bot, button(session, "🗓 В календарь"))

    said = session.texts[-1]
    assert "Запись цела" in said
    assert "Refresh token отозван." in said


async def test_a_calendar_that_is_off_says_so_instead_of_failing(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
    offered: None,
) -> None:
    """Ненастроенный календарь — состояние, а не ошибка."""
    recorder = Calls(events.Result(outcome=events.Outcome.OFF))
    monkeypatch.setattr(events, "create_for_entry", recorder.create)

    await capture(dispatcher, bot)
    await press(dispatcher, bot, button(session, "🗓 В календарь"))

    assert "не настроен" in session.texts[-1]


# --- Никакого другого пути наружу ---


def test_nothing_else_in_the_source_creates_events() -> None:
    """Критерий фазы, проверенный по исходникам, а не по памяти автора.

    Тест пути доказывает, что нажатие событие создаёт. Он **не** доказывает,
    что его не создаёт что-нибудь ещё, — а именно это обещано владельцу
    дословно: «ни одно событие не создано без моего нажатия».

    Поэтому здесь проверка обратная и грубая: во всём `src/` вызов
    `create_for_entry` встречается ровно в двух местах — определении
    и обработчике нажатия. Появится третье — тест покраснеет, и автор объяснит,
    зачем оно, вместо того чтобы это выяснилось на живом календаре.
    """
    root = Path(__file__).resolve().parent.parent / "src"
    callers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "create_for_entry" in path.read_text(encoding="utf-8")
    )

    assert callers == [
        "brain/adapters/telegram/handlers.py",
        "brain/integrations/gcal/events.py",
    ]
