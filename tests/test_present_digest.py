"""Утреннюю сводку пишет модель — от тика касаний до текста у отправителя.

Задача 7 волны ADR-0015. До неё сводка была склейкой строк из базы: заголовок,
нумерованный список и вопрос под петлёй. Ни одна строка не была написана
для владельца, и ревизия после недели использования назвала это главной
причиной, по которой сводку перестают читать.

**Почему тесты идут от `service.tick`, а не от `digest.write`.** Тест на функцию
не заменяет теста на путь: `loops.is_stuck` была написана, покрыта тестом
и не звалась из `src/` ни разу. Здесь цепочка длиннее — расписание, отбор дел,
сборка входа, вызов модели, проверка гварда, рендер, отправитель, — и порваться
она может в любом месте. Поэтому утверждения делаются про текст, ушедший
в `FakeNotifier`, и про промпт, дошедший до модели.

Модель заменена заглушкой намеренно и честно: все утверждения файла — про путь,
про отбор входа и про отказ, а не про качество лида. Качество меряет владелец
живым прогоном (`just eval-digest`): суточный лимит бесплатного тира — двадцать
запросов на модель.

Тексты живые, по номерам записей (ADR-0002: архив в git не уходит).
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import Settings, get_settings
from brain.core.digest import loops, render, service
from brain.core.digest.morning import COMMITMENTS_SHOWN, Morning
from brain.core.memory import overrides
from brain.core.ports import Completion
from brain.core.present import digest as summary
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.loop import Loop
from tests import archive as archive_module
from tests.fakes import FakeNotifier

TIMEZONE = "Europe/Rome"
CHAT_ID = 111

# 09:30 по Риму 20 августа 2026 — внутри утреннего окна. Тот же момент, что
# в `test_digest_tick` и `test_present_titles`: сводка приходит там же, где
# и раньше, меняется только текст.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
# Вчера по календарю владельца — 19 августа. Момент внутри дня, а не полночь:
# граница суток считается в его таймзоне, и запись, сделанная в час ночи
# по Риму, вчерашней быть не должна.
YESTERDAY = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

# Живая запись, из которой режутся дела. Та же, что в тестах названий: в ней
# два дела подряд и настоящая граница между ними.
LIVE_ENTRY = 53
FILM = "Фильм, да, надо будет посмотреть"
BOOK = "Книгу у меня опять не получилось вчера почитать"

# Живая запись вчерашнего дня — тот контекст, ради которого задача и затеяна.
# Фраза для проверки выбрана редкая: она не встречается больше нигде в архиве.
LIVE_YESTERDAY = 62
YESTERDAY_MARK = "игру Плистолов"

LEAD = "Книга третий день ждёт — почитать сегодня или снять её с висящих?"


class Summariser:
    """Модель, которая пишет лид по сценарию и помнит, о чём её спросили.

    Помнить важнее, чем отвечать: главные утверждения файла — «во входе есть
    вчерашний контекст», «во входе нет счётчика откладываний» и «приватное
    наружу не ушло» — проверяются промптом, а не ответом.
    """

    def __init__(
        self,
        lead: str = LEAD,
        *,
        answer: dict[str, Any] | None = None,
        fails: bool = False,
    ) -> None:
        self._lead = lead
        self._answer = answer
        self._fails = fails
        self.prompts: list[str] = []
        self.systems: list[str] = []

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-digest"

    @property
    def tier(self) -> str:
        return "cheap"

    @property
    def calls(self) -> int:
        return len(self.prompts)

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        temperature: float,
    ) -> Completion:
        self.systems.append(system)
        self.prompts.append(prompt)
        if self._fails:
            raise RuntimeError("провайдер не ответил")
        data = self._answer if self._answer is not None else {"lead": self._lead}
        return Completion(data=data, input_tokens=10, output_tokens=10, latency_ms=1)


# --- Настройки. Быстрые тесты, без базы и без сети ---


def test_digest_model_is_its_own_setting() -> None:
    """Модель сводки не наследует модель чистки и модель названий.

    У каждой задачи слоя своя, как у `CLEANUP_MODEL` и `TITLES_MODEL`: лид
    зовётся раз в сутки, а чистка — на каждое голосовое, и снимать их под
    квотой надо порознь.
    """
    assert Settings(cleanup_model="gemini-3.5-flash-lite").digest_model == "gemini-3.6-flash"


def test_digest_order_defaults_to_what_was_there_before() -> None:
    """Порядок выбирается наблюдением, а не автором, — поэтому дефолт прежний.

    Настройка появляется сейчас, значение остаётся за владельцем: «дела или
    петля первыми» решается несколькими днями использования (ADR-0015, цена).
    """
    assert Settings().digest_order == "deeds_first"


async def test_zero_data_means_zero_calls() -> None:
    """Нечего показать — модель не зовётся вовсе.

    Не ради экономии: сводка ни о чём хуже отсутствия сводки, и вызов ради
    пустого сообщения потратил бы запрос из двадцати суточных.
    """
    model = Summariser()

    written = await summary.write(
        model, deeds=[], question=None, now=MORNING, timezone=TIMEZONE, words=40
    )

    assert written is None
    assert model.calls == 0


def test_order_is_a_setting_not_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Что первым — дела или петля — выбирается наблюдением, а не автором."""
    monkeypatch.setenv("DIGEST_ORDER", "loop_first")
    get_settings.cache_clear()

    text = render.morning(_content_with_a_loop())

    assert text.index("🔁") < text.index("1.")


def test_deeds_come_first_while_the_setting_says_so() -> None:
    """Дефолт не меняет ничего молча: сводка выглядит как до задачи."""
    text = render.morning(_content_with_a_loop())

    assert text.index("1.") < text.index("🔁")


def _content_with_a_loop() -> Morning:
    return Morning(
        commitments=[Commitment(id=1, text=FILM, state=CommitmentState.OPEN)],
        open_commitments=1,
        loop=Loop(id=1, question="куда дальше", times_asked=0),
    )


# --- Путь целиком: от тика до текста у отправителя ---


async def _entry(
    url: str, entries: list[archive_module.Entry], number: int, *, created_at: datetime
) -> int:
    """Положить живую запись в одноразовую базу нужным днём."""
    source = archive_module.entry(entries, number)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.TEXT,
                    raw_text=source.text,
                    is_private=source.is_private,
                    timezone=TIMEZONE,
                    created_at=created_at,
                    dedup_key=f"live:{number}:{created_at.isoformat()}",
                    meta={"chat_id": CHAT_ID, "ack_message_id": 1},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _deed(
    url: str, entry_id: int, text: str, *, days: int, span: int = 0, title: str | None = None
) -> int:
    """Дело по живой записи, заведённое столько-то дней назад."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            deed_id = await connection.scalar(
                insert(Commitment)
                .values(
                    source_entry_id=entry_id,
                    source_unit_id=None,
                    text=text,
                    title=title,
                    span_start=span,
                    span_end=span + len(text),
                    state=CommitmentState.OPEN,
                    created_at=MORNING - timedelta(days=days),
                )
                .returning(Commitment.id)
            )
        return int(deed_id or 0)
    finally:
        await engine.dispose()


async def _tick(model: Summariser | None, notifier: FakeNotifier) -> None:
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=model
    )


@pytest.mark.integration
async def test_the_model_writes_the_lead_of_the_summary(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Главное утверждение задачи: в сводке появилась строка, написанная для владельца.

    Дела и петля при этом остаются на месте и остаются кодом: модель пишет
    только то, чего код не умеет.
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3, title="Почитать книгу")
    await _deed(migrated_database, entry_id, FILM, days=2, span=100)
    model = Summariser()
    notifier = FakeNotifier()

    await _tick(model, notifier)

    _, text, _, _ = notifier.mornings[0]
    assert LEAD in text
    assert "Почитать книгу" in text
    assert model.calls == 1


@pytest.mark.integration
async def test_yesterdays_records_go_to_the_model_with_their_text(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Ради этого задача и существует: модель должна уметь связать.

    Скупой вход даёт скупой текст, поэтому вчерашнее уходит контекстом,
    а не списком названий.
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3)
    await _entry(migrated_database, archive, LIVE_YESTERDAY, created_at=YESTERDAY)
    model = Summariser()

    await _tick(model, FakeNotifier())

    assert YESTERDAY_MARK in model.prompts[0]


@pytest.mark.integration
async def test_the_snooze_count_never_reaches_the_model(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Счётчик откладываний не передаётся сознательно.

    «Сдвинулось?» на первой петле — нормальный вопрос, на четвёртой —
    требование отчёта у человека, который трижды ответил «ещё думаю». Гвард
    ловит упрёк по лексике и упрёк по счётчику поймать не может в принципе:
    не зная счётчика, модель не может им попрекнуть.

    Вопрос при этом меняется — его выбирает код, как и раньше.
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3)
    loop_id = await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше с ботом")

    moment = MORNING - loops.INTERVAL * 4
    for _ in range(loops.SNOOZES_BEFORE_DECISION):
        await loops.mark_asked(loop_id, now=moment)
        await loops.still_thinking(loop_id, now=moment)
        moment += loops.INTERVAL

    model = Summariser()
    notifier = FakeNotifier()

    await _tick(model, notifier)

    loop_line = next(
        line for line in model.prompts[0].splitlines() if f"] {summary.LOOP_MARK} · " in line
    )
    assert loop_line.endswith("куда дальше с ботом")
    assert "откладыв" not in model.prompts[0]

    _, text, _, _ = notifier.mornings[0]
    assert "Закрыть или переформулируешь?" in text


@pytest.mark.integration
async def test_a_private_entry_never_reaches_the_summary_model(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Инвариант 5 держится дверью и отбором, а не вызывающим кодом.

    Приватное дело при этом из сводки не исчезает: наружу ничего не уходит,
    а владельцу его собственная запись показывается — это его архив и его бот.
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3)
    secret_id = await _entry(migrated_database, archive, LIVE_YESTERDAY, created_at=YESTERDAY)
    await overrides.make_private(secret_id)
    # Дело по приватной записи — её же словами: и запись, и выведенное из неё
    # дело обязаны остаться внутри, а проверяется это одной редкой фразой.
    await _deed(migrated_database, secret_id, f"Посмотреть {YESTERDAY_MARK}", days=1)
    model = Summariser()
    notifier = FakeNotifier()

    await _tick(model, notifier)

    assert YESTERDAY_MARK not in model.prompts[0]

    _, text, _, _ = notifier.mornings[0]
    assert YESTERDAY_MARK in text


@pytest.mark.integration
async def test_a_summary_of_only_private_deeds_costs_no_call(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Всё сегодняшнее приватно — вызова нет вовсе.

    Позвать модель было бы хуже, чем не позвать: она получила бы вчерашний
    контекст без единого дела и написала бы лид о том, чего в сообщении нет.
    """
    secret_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await overrides.make_private(secret_id)
    await _deed(migrated_database, secret_id, BOOK, days=3)
    # Вчерашний контекст при этом есть и приватным не стал: пачка непустая,
    # и вызов случился бы, не будь этой проверки.
    await _entry(migrated_database, archive, LIVE_YESTERDAY, created_at=YESTERDAY)
    model = Summariser()
    notifier = FakeNotifier()

    await _tick(model, notifier)

    assert model.calls == 0
    _, text, _, _ = notifier.mornings[0]
    assert BOOK in text


@pytest.mark.integration
async def test_the_dry_render_comes_when_the_model_says_nothing(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Отказ модели стоит лида, а не сводки. Сухой рендер не удаляется никогда."""
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3, title="Почитать книгу")
    await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше с ботом")
    notifier = FakeNotifier()

    await _tick(Summariser(fails=True), notifier)

    _, text, _, loop_id = notifier.mornings[0]
    assert text.startswith("☀️ Утро.")
    assert "Почитать книгу" in text
    assert "куда дальше с ботом" in text
    assert loop_id is not None


@pytest.mark.integration
async def test_the_dry_render_comes_when_the_guard_refuses_the_lead(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Упрёк без выбора до владельца не доезжает — и второй попытки нет.

    Вторая попытка стоит второго запроса под квотой в момент, когда первый уже
    показал, что что-то не так (`present/guard.py`).
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3, title="Почитать книгу")
    model = Summariser("Книгу ты так и не открыл.")
    notifier = FakeNotifier()

    await _tick(model, notifier)

    _, text, _, _ = notifier.mornings[0]
    assert "так и не открыл" not in text
    assert "Почитать книгу" in text
    assert model.calls == 1


@pytest.mark.integration
async def test_the_lead_with_an_invented_number_is_refused(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Дата, взявшаяся из ниоткуда, в сводке неотличима от настоящей."""
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3, title="Почитать книгу")
    notifier = FakeNotifier()

    await _tick(Summariser("Книгу ты обещал закрыть к 19 декабря."), notifier)

    _, text, _, _ = notifier.mornings[0]
    assert "декабря" not in text


@pytest.mark.integration
async def test_the_whole_summary_fits_forty_seconds(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Сорок секунд — проверяемый бюджет, а не пожелание.

    Меряется всё сообщение целиком, а не один лид: владелец читает то, что
    пришло, а не то, что написала модель.
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    for number in range(3):
        await _deed(migrated_database, entry_id, BOOK, days=3 + number, span=number * 100)
    await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше с ботом")
    notifier = FakeNotifier()

    await _tick(Summariser("слово " * 200), notifier)

    _, text, _, _ = notifier.mornings[0]
    assert len(text.split()) <= summary.WORD_BUDGET


@pytest.mark.integration
async def test_the_summary_asks_for_no_more_than_three_deeds(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Не больше трёх действий (ADR-0015). Остальные — по кнопке «все дела»."""
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    for number in range(5):
        await _deed(migrated_database, entry_id, BOOK, days=3 + number, span=number * 100)
    notifier = FakeNotifier()

    await _tick(Summariser(), notifier)

    _, _, commitment_ids, _ = notifier.mornings[0]
    assert len(commitment_ids) == summary.MAX_DEEDS == COMMITMENTS_SHOWN == 3


@pytest.mark.integration
async def test_the_lead_is_held_back_under_quota(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Косметика замолкает первой — по наблюдённому отказу, а не по лимиту.

    Сводка при этом приходит: сухим рендером, как до волны (ADR-0015, п. 6).
    """
    entry_id = await _entry(migrated_database, archive, LIVE_ENTRY, created_at=YESTERDAY)
    await _deed(migrated_database, entry_id, BOOK, days=3, title="Почитать книгу")
    await _refusal(migrated_database)
    model = Summariser()
    notifier = FakeNotifier()

    await _tick(model, notifier)

    assert model.calls == 0
    _, text, _, _ = notifier.mornings[0]
    assert "Почитать книгу" in text


async def _refusal(url: str) -> None:
    """Отказ по квоте в текущих сутках провайдера — как его пишет дверь."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(LlmCall).values(
                    provider="gemini",
                    model="stub-digest",
                    tier="cheap",
                    purpose="cleanup",
                    entries_count=1,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=1,
                    succeeded=False,
                    status_code=429,
                    created_at=MORNING - timedelta(minutes=5),
                )
            )
    finally:
        await engine.dispose()
