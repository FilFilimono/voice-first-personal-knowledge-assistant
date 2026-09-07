"""Приватный режим: поиск идёт, но наружу не выходит.

**Решение владельца 7 сентября 2026**, довод дословно: *несогласованность двух
дверей хуже любой из них — привыкнет к одной, ошибётся на другой.* До этого дня
двери вели себя по-разному: ответ по архиву на обычное сообщение в приватном
режиме не искал вовсе, а команда `/find` искала — и отправляла запрос наружу
за вектором, то есть ровно в том режиме, который заведён затем, чтобы наружу
не уходило ничего.

Отказывать целиком не надо: **два канала из трёх наружу не ходят.**
Полнотекст и триграммы считаются в Postgres, вектор — внешней моделью.
Значит, приватный режим снимает третий канал, а не поиск.

Что здесь проверяется, и всё это — про путь, а не про функцию:

    1. в приватном режиме эмбеддер не строится и не зовётся ни разу;
    2. найденное всё равно показывается — поиск работает двумя каналами;
    3. владельцу сказано, что вектора не было: выдача без смысловой близости
       беднее, и молча выдавать её за полную нельзя;
    4. обе двери — команда `/find` и обычный вопрос — ведут себя одинаково.

Быстрые тесты: база и сеть сюда не поднимаются.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram import search_text
from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.retrieval.search import Found
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import OWNER_ID, FakeSession, make_bot, make_message, make_update

STUDY = Found(
    entry_id=42,
    created_at=datetime(2026, 8, 6, 9, 30, tzinfo=UTC),
    timezone="Europe/Rome",
    is_private=False,
    text="Надо всё-таки взяться за курсовую, иначе в сентябре будет то же самое.",
    score=0.02,
    channels=("fulltext", "trigram"),
)


class LocalSpy:
    """Поиск без вектора. Помнит, о чём спросили."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __call__(self, query: str, *, limit: int = 5) -> list[Found]:
        self.queries.append(query)
        return [STUDY]


class VectorSpy:
    """Обычный поиск. В приватном режиме его не должно быть вовсе."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __call__(self, embedder: object, query: str, *, limit: int = 5) -> list[Found]:
        self.queries.append(query)
        return [STUDY]


class EmbedderSpy:
    """Считает, сколько раз собрались идти наружу за вектором."""

    def __init__(self) -> None:
        self.built = 0

    def __call__(self, settings: object) -> object:
        self.built += 1
        return object()


@pytest.fixture
def local() -> LocalSpy:
    return LocalSpy()


@pytest.fixture
def vector() -> VectorSpy:
    return VectorSpy()


@pytest.fixture
def embedder() -> EmbedderSpy:
    return EmbedderSpy()


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    local: LocalSpy,
    vector: VectorSpy,
    embedder: EmbedderSpy,
) -> Dispatcher:
    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield FakeUnitOfWork()

    async def private() -> bool:
        return True

    async def no_topic(name: str, now: datetime) -> None:
        """Тем в быстром тесте нет: разговор по теме в базу не полезет."""
        return None

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", private)
    monkeypatch.setattr("brain.adapters.telegram.handlers.search.search", vector)
    monkeypatch.setattr("brain.adapters.telegram.handlers.search.local", local)
    monkeypatch.setattr("brain.adapters.telegram.handlers.archive_topics.collect", no_topic)
    monkeypatch.setattr("brain.adapters.telegram.handlers.embeddings.build_embedder", embedder)

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


async def test_find_in_private_mode_never_goes_outside(
    dispatcher: Dispatcher,
    bot: Bot,
    local: LocalSpy,
    vector: VectorSpy,
    embedder: EmbedderSpy,
) -> None:
    """Главный тест файла: за вектором не пошли ни разу."""
    await dispatcher.feed_update(bot, make_update(make_message(text="/find курсовая")))

    assert local.queries == ["курсовая"]
    assert vector.queries == []
    assert embedder.built == 0, "эмбеддер построен — значит запрос собирался наружу"


async def test_find_in_private_mode_still_finds(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Отказывать целиком не надо: два канала из трёх работают и здесь."""
    await dispatcher.feed_update(bot, make_update(make_message(text="/find курсовая")))

    said = session.texts[-1]
    assert "курсовую" in said, said


async def test_the_owner_is_told_the_search_was_narrower(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Выдача без смысловой близости беднее — молча выдавать её за полную нельзя.

    Это то же правило, по которому ретроактивная приватность обязана сказать,
    что текст уже уходил наружу: обещать полноту, которой нет, хуже, чем
    признать неполноту.
    """
    await dispatcher.feed_update(bot, make_update(make_message(text="/find курсовая")))

    assert search_text.WITHOUT_VECTOR in session.texts[-1]


async def test_a_plain_question_behaves_the_same_way(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    local: LocalSpy,
    vector: VectorSpy,
    embedder: EmbedderSpy,
) -> None:
    """Вторая дверь. Именно её несогласованность с первой и была претензией.

    Обычное сообщение при этом **сохраняется** — как и всегда: ответ по архиву
    добавляется к записи, а не заменяет её.
    """
    await dispatcher.feed_update(bot, make_update(make_message(text="что я думал про курсовую?")))

    assert local.queries, "вторая дверь не искала вовсе"
    assert vector.queries == []
    assert embedder.built == 0
    assert search_text.WITHOUT_VECTOR in session.texts[-1]
