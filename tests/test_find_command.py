"""Что должно работать:

1. `/find` возвращает найденное, и у каждой строки есть дата
2. `/find` без запроса объясняет, как им пользоваться, а не молчит
3. ничего не нашлось — так и говорит
4. обычное сообщение **не теряется** из-за того, что похоже на вопрос
5. упавший поиск объясняется вслух и не выглядит как пустой архив
6. приватная запись в выдаче помечена

Пункт 4 — главный, и 06.09.2026 он переформулирован. Раньше он звучал
«обычное сообщение не перехватывается поиском», и поиск жил только командой:
слово «найди» в начале мысли считалось мыслью. Владелец отверг это прямо —
«ты должен зайти, прочитать и вспомнить, а он тебе даже этого не даёт», —
и теперь похожее на вопрос сообщение отвечается поиском тоже.

Неизменной осталась причина, по которой пункт главный: захват — единственное,
что в этой системе не имеет права работать через раз. Поэтому поиск
**добавляется** к записи, а не заменяет её, и ложное срабатывание признака
стоит лишнего сообщения, а не мысли. Разбор признака и его цена —
`tests/test_archive_talk.py`.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher

from brain.adapters.telegram import search_text
from brain.adapters.telegram.bot import COMMANDS, build_dispatcher
from brain.config import Settings
from brain.core.retrieval.search import Found
from tests.fakes import FakeUnitOfWork
from tests.telegram_harness import OWNER_ID, FakeSession, make_bot, make_message, make_update

TODAY = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

# Найденное, каким его отдаёт `core/retrieval`. Тексты — из базы дословно.
DISCIPLINE = Found(
    entry_id=1,
    created_at=datetime(2026, 8, 16, 9, 30, tzinfo=UTC),
    timezone="Europe/Moscow",
    is_private=False,
    text=(
        "Так, ну первая запись в моём боте боте втором мозге как бы сейчас мы его "
        "доделываем, я ещё пока что ничего не разбирал по этому проекту."
    ),
    score=0.03,
    channels=("fulltext", "vector"),
)
OFFICE = Found(
    entry_id=28,
    # 21:00 по Москве 31 декабря — то есть ещё прошлый год. Взято близко
    # к границе намеренно: 21:00 UTC того же дня в Москве уже 1 января,
    # и дата обязана считаться в таймзоне записи, а не сервера.
    created_at=datetime(2025, 12, 31, 18, 0, tzinfo=UTC),
    timezone="Europe/Moscow",
    is_private=True,
    text="напомни мне уйти в 19:40 вечера домой",
    score=0.02,
    channels=("trigram",),
)


class SearchSpy:
    """Поиск, который ничего не ищет, но помнит, о чём спросили."""

    def __init__(self, result: list[Found] | None = None, fails: bool = False) -> None:
        self.result = result if result is not None else []
        self.fails = fails
        self.queries: list[str] = []

    async def __call__(self, embedder: object, query: str, *, limit: int = 5) -> list[Found]:
        self.queries.append(query)
        if self.fails:
            raise RuntimeError("провайдер эмбеддингов лежит")
        return self.result


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def searcher() -> SearchSpy:
    return SearchSpy([DISCIPLINE, OFFICE])


@pytest.fixture
def dispatcher(
    monkeypatch: pytest.MonkeyPatch, uow: FakeUnitOfWork, searcher: SearchSpy
) -> Dispatcher:
    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield uow

    async def not_private() -> bool:
        return False

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", not_private)

    async def no_topic(name: str, now: datetime) -> None:
        """Тем в быстром тесте нет: база сюда не поднимается.

        Подменять обязательно. Без этого разговор по теме полез бы в базу,
        упал бы там и увёл ответ в «Поиск не отработал» — то есть тест был бы
        зелёным по неправильной причине, а поиск не проверялся бы вовсе.
        """
        return None

    monkeypatch.setattr("brain.adapters.telegram.handlers.search.search", searcher)
    monkeypatch.setattr("brain.adapters.telegram.handlers.archive_topics.collect", no_topic)
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.embeddings.build_embedder", lambda settings: object()
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


async def test_find_returns_results(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, searcher: SearchSpy
) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text="/find дисциплина")))

    assert searcher.queries == ["дисциплина"]
    assert "первая запись" in session.texts[-1]


async def test_every_result_has_a_date(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Без даты найденная фраза одинаково похожа на вчерашнюю и прошлогоднюю."""
    await dispatcher.feed_update(bot, make_update(make_message(text="/find дисциплина")))

    answer = session.texts[-1]
    assert "16.08" in answer
    assert "31.12.2025" in answer, "у прошлогодней записи должен быть год"


async def test_private_result_is_marked(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text="/find дисциплина")))

    assert "🔒" in session.texts[-1]


async def test_find_without_query_explains_itself(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, searcher: SearchSpy
) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text="/find")))

    assert session.texts[-1] == search_text.EMPTY_QUERY
    assert searcher.queries == [], "пустой запрос не должен стоить вызова провайдера"


async def test_nothing_found_says_so(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, searcher: SearchSpy
) -> None:
    searcher.result = []

    await dispatcher.feed_update(bot, make_update(make_message(text="/find чепуха")))

    assert search_text.NOTHING.lower() in session.texts[-1]


async def test_broken_search_is_not_an_empty_archive(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, searcher: SearchSpy
) -> None:
    """«Ничего не нашёл» после падения провайдера — вранье, и опасное."""
    searcher.fails = True

    await dispatcher.feed_update(bot, make_update(make_message(text="/find дисциплина")))

    assert "Записи целы" in session.texts[-1]
    assert search_text.NOTHING.lower() not in session.texts[-1]


async def test_ordinary_message_is_still_captured(
    dispatcher: Dispatcher, bot: Bot, uow: FakeUnitOfWork, searcher: SearchSpy
) -> None:
    """Слово «найди» в начале мысли уводит в поиск — но записи не стоит.

    С 06.09.2026 «найди» входит в список признаков вопроса к архиву
    (`present/archive_question.OPENINGS`), и на этой фразе признак срабатывает
    **зря**: «найди время на разбор задач» — задача, а не вопрос. Цена ошибки
    названа здесь целиком: владелец получит лишнее сообщение с выдачей поиска,
    а мысль всё равно ляжет записью. Обратный порядок был бы недопустим —
    `entries` только на вставку.
    """
    await dispatcher.feed_update(bot, make_update(make_message(text="найди время на разбор задач")))

    assert uow.entries[0].raw_text == "найди время на разбор задач"
    assert searcher.queries == ["время на разбор задач"]


async def test_command_is_advertised() -> None:
    """Команда, которой нет в подсказке, для владельца не существует."""
    assert any(command.command == "find" for command in COMMANDS)


# --- Разметка отдельно от бота ---


def test_render_keeps_the_query_visible() -> None:
    rendered = search_text.render("дисциплина", [DISCIPLINE], today=TODAY)

    assert "дисциплина" in rendered


def test_render_shortens_long_text_on_word_boundary() -> None:
    long_entry = Found(
        entry_id=31,
        created_at=TODAY,
        timezone="Europe/Moscow",
        is_private=False,
        text="слово " * 100,
        score=0.0,
        channels=(),
    )

    line = search_text.render("что угодно", [long_entry], today=TODAY).splitlines()[-1]

    # Якорь `/e31` стоит **после** цитаты (решение владельца 07.09.2026),
    # поэтому обрезка проверяется до него, а не по концу строки.
    quoted = line.split("  ")[0]

    assert quoted.endswith("…")
    assert "слов…" not in quoted, "обрезано посреди слова"


def test_render_collapses_line_breaks() -> None:
    """Перевод строки внутри записи разорвал бы список на две строки."""
    entry = Found(
        entry_id=5,
        created_at=TODAY,
        timezone="Europe/Moscow",
        is_private=False,
        text="первая строка\nвторая строка",
        score=0.0,
        channels=(),
    )

    rendered = search_text.render("что угодно", [entry], today=TODAY)

    assert len(rendered.splitlines()) == 3  # заголовок, пустая, одна запись


async def test_search_is_awaited_once_per_command(
    dispatcher: Dispatcher, bot: Bot, searcher: SearchSpy
) -> None:
    """Два запроса — два поиска, и ни одного лишнего вызова провайдера."""
    await dispatcher.feed_update(bot, make_update(make_message(text="/find раз")))
    await dispatcher.feed_update(bot, make_update(make_message(text="/find два")))
    await asyncio.sleep(0)

    assert searcher.queries == ["раз", "два"]
