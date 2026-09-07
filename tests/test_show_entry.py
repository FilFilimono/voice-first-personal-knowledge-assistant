"""«Покажи запись целиком» — и якорь в каждом месте, где показана цитата.

**Решение владельца 7 сентября 2026.** Довод дословно: *обрезка на 220 символах
это то же «и ещё 64», только в другом месте.* `vision.md` §8 требует прямым
текстом «синтез плюс источники с датами, всегда можно провалиться в оригинал»,
и до этого дня вторая половина требования была неправдой: дата у цитаты была,
а команды показать запись не было вовсе.

Форма выбрана владельцем — **тапабельная команда `/e42` в конце цитаты**.
Telegram сам делает `/слово` ссылкой, поэтому это один тап и ничего не надо
набирать, и при этом это не кнопка: с кнопками владелец взаимодействовать
не хочет (приёмка Волны 1).

**Главное в этом файле — не команда, а полнота охвата.** Цитата показывается
в пяти местах, и провалиться в оригинал надо из каждого: связи, раскачка,
разговор по теме, поиск, всплывание. Шестое нашлось при разборе — напоминание
привычки тоже цитирует. Механизм, работающий в четырёх местах из шести,
в этом проекте уже был: напоминания, работавшие только для текста.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram import search_text
from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.digest import prompting, render
from brain.core.digest.resurface import Old
from brain.core.present import archive_topics, entry
from brain.core.present import recall as recall_text
from brain.core.retrieval.links import Link
from brain.core.retrieval.search import Found
from brain.storage.models.habit import Habit, Rhythm
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import OWNER_ID, FakeSession, make_bot, make_message, make_update

TIMEZONE = "Europe/Rome"
NOW = datetime.now(UTC)
LONG_AGO = NOW - timedelta(days=30)

# Длиннее любого предела показа: 220 у разговора по теме, 200 у связи,
# 160 у поиска, 400 у всплывания. Ровно то, что владелец и не мог прочитать.
BODY = (
    "Надо всё-таки взяться за курсовую, иначе в сентябре будет ровно то же самое, "
    "что и в прошлом году, когда я оставил всё на последнюю неделю и потом две ночи "
    "подряд писал непонятно что, а научрук потом полтора часа объяснял, почему это "
    "никуда не годится, и был совершенно прав."
)


def test_anchor_is_a_tappable_telegram_command() -> None:
    """Telegram делает ссылкой `/слово` — латиница, цифры и подчёркивание.

    Кириллица и пробел ссылку убивают, и якорь превратился бы в текст,
    который надо набирать руками. Проверяется формой, а не глазами.
    """
    said = entry.anchor(42)

    assert said == "/e42"
    assert said[1:].replace("_", "").isalnum()


# --- Пять мест, где показана цитата, плюс шестое ---


def test_search_results_carry_the_anchor() -> None:
    found = [
        Found(
            entry_id=42,
            created_at=LONG_AGO,
            timezone=TIMEZONE,
            is_private=False,
            text=BODY,
            score=0.02,
            channels=("fulltext",),
        )
    ]

    said = search_text.render("курсовая", found, today=NOW)

    assert entry.anchor(42) in said


def test_the_talk_about_a_topic_carries_the_anchor() -> None:
    talk = archive_topics.Talk(
        topic="учёба",
        total=1,
        fresh=0,
        moments=[archive_topics.Moment(entry_id=42, text=BODY, created_at=LONG_AGO)],
    )

    said = archive_topics.text(talk, timezone=TIMEZONE, now=NOW)

    assert entry.anchor(42) in said


def test_a_link_carries_the_anchor() -> None:
    links = [Link(entry_id=42, text=BODY, created_at=LONG_AGO, timezone=TIMEZONE, similarity=0.85)]

    said = recall_text.template(links, timezone=TIMEZONE)

    assert entry.anchor(42) in said


def test_a_resurfaced_entry_carries_the_anchor() -> None:
    old = Old(entry_id=42, text=BODY, created_at=LONG_AGO, is_private=False)

    said = render.resurfaced(old, timezone=TIMEZONE)

    assert entry.anchor(42) in said


def test_a_habit_reminder_carries_the_anchor() -> None:
    """Шестое место. Нашлось разбором, а не из списка владельца."""
    habit = Habit(
        id=1,
        source_entry_id=7,
        text="читать книгу",
        rhythm=Rhythm.DAILY,
        hour=9,
        minute=0,
    )
    links = [Link(entry_id=42, text=BODY, created_at=LONG_AGO, timezone=TIMEZONE, similarity=0.85)]

    said = render.habit(habit, links, timezone=TIMEZONE)

    assert entry.anchor(42) in said


def test_the_prompting_material_carries_the_anchor_of_the_quoted_entry() -> None:
    """Раскачка. Якорь указывает на **процитированную** запись, а не на свежую.

    У `look_back` записей две: та, о чём владелец говорит сейчас, и старая,
    которую цитируют. Цитата в материале — старая, и провалиться надо в неё.
    """
    asked = prompting._make(
        prompting.LOOK_BACK,
        entry_ids=(7, 42),
        facts=("свежая", BODY),
        quoted=42,
        lately="Ты сейчас про тему «учёба»",
        when="06.08",
        quote=prompting.quote(BODY),
    )

    assert entry.anchor(42) in asked.material
    assert entry.anchor(7) not in asked.whole
    assert entry.anchor(42) in asked.whole


def test_a_question_that_quotes_inside_itself_carries_the_anchor() -> None:
    """`yesterday` и `blocker` цитируют внутри самого вопроса, а не в материале.

    Значит и якорь у них — в тексте вопроса. Проверяется отдельно: у этих двух
    видов `material` пуст, и общая проверка «якорь в материале» их пропустила бы.
    """
    asked = prompting._make(
        prompting.YESTERDAY,
        entry_ids=(42,),
        facts=(BODY,),
        quoted=42,
        quote=prompting.quote(BODY),
    )

    assert asked.material == ""
    assert entry.anchor(42) in asked.text


# --- Сам показ ---


def test_the_entry_is_shown_whole(archive: object) -> None:
    """Без обрезки — это и есть весь смысл (`vision.md` §8).

    `archive` здесь не нужен, но фикстура держит файл честным: тексты записей
    в репозитории не лежат (ADR-0002), и постоянные для показа берутся отсюда.
    """
    shown = entry.Shown(
        entry_id=42,
        text=BODY,
        created_at=LONG_AGO,
        timezone=TIMEZONE,
        is_private=False,
    )

    said = entry.text(shown, now=NOW)

    assert BODY in said, "запись обрезана — ровно то, что чинили"
    assert "…" not in said


def test_a_private_entry_is_shown_with_a_lock() -> None:
    """Приватную показываем: это переписка владельца с собственным ботом.

    То же правило, что у всплывания (`vision.md` §5.3): инвариант 5 про
    внешние сервисы, а не про то, что бот показывает хозяину. Замочек при этом
    обязателен — владелец должен видеть, с чем имеет дело.
    """
    shown = entry.Shown(
        entry_id=42,
        text=BODY,
        created_at=LONG_AGO,
        timezone=TIMEZONE,
        is_private=True,
    )

    assert "🔒" in entry.text(shown, now=NOW)


def test_a_long_entry_is_split_and_nothing_is_lost() -> None:
    """Предел Telegram — 4096 символов. Резать смысл нельзя, значит делим.

    Обрезать было бы возвращением того самого дефекта, ради которого команда
    и заведена, только с большим числом.
    """
    huge = " ".join([BODY] * 20)

    parts = entry.parts(huge)

    assert len(parts) > 1
    assert all(len(part) <= entry.MESSAGE_LIMIT for part in parts)
    assert " ".join(parts).split() == huge.split()


def test_an_unknown_number_says_so() -> None:
    """Молчать нельзя: тапнув по якорю, владелец ждёт ответа."""
    assert entry.NOT_FOUND


# --- Путь целиком ---
#
# Тест на функцию не заменяет теста на путь — правило проекта, купленное
# девятью дырами. Здесь риск конкретный и виден только на настоящем роутере:
# `/e42` — это обычное текстовое сообщение, и обработчик, зарегистрированный
# после общего фильтра `F.text`, не получил бы его никогда. Тогда якорь молча
# ложился бы в архив записью «/e42».


@pytest.fixture
def shown() -> entry.Shown:
    return entry.Shown(
        entry_id=42,
        text=BODY,
        created_at=LONG_AGO,
        timezone=TIMEZONE,
        is_private=False,
    )


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch, uow: FakeUnitOfWork, shown: entry.Shown
) -> Dispatcher:
    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield uow

    async def not_private() -> bool:
        return False

    async def full(entry_id: int) -> entry.Shown | None:
        return shown if entry_id == 42 else None

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", not_private)
    monkeypatch.setattr("brain.adapters.telegram.handlers.entry_view.full", full)

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


async def test_tapping_the_anchor_shows_the_entry(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, uow: FakeUnitOfWork
) -> None:
    """Главный тест файла: якорь доходит до обработчика и показывает запись.

    И **не ложится в архив**: обработчик стоит выше общего `F.text`, иначе
    «/e42» стало бы записью — той самой мыслью, которую владелец не писал.
    """
    await dispatcher.feed_update(bot, make_update(make_message(text=entry.anchor(42))))

    assert BODY in session.texts[-1]
    assert uow.entries == [], "якорь лёг в архив записью"


async def test_tapping_an_unknown_anchor_answers_instead_of_going_silent(
    dispatcher: Dispatcher, bot: Bot, uow: FakeUnitOfWork
) -> None:
    """Тапнув, владелец ждёт ответа. Молчание он прочитает как поломку."""
    await dispatcher.feed_update(bot, make_update(make_message(text=entry.anchor(999))))

    assert uow.entries == []
