"""Разговор с архивом: вопрос без команды и рассказ по теме.

Претензия владельца 06.09.2026, дословно: «второй мозг, куда записываешь мысли.
Но я их все забыл, и он мне их не напомнит. Ты должен зайти, прочитать
и вспомнить — а он тебе даже этого не даёт».

Что должно работать:

 1. обычное сообщение, похожее на вопрос, отвечается поиском по архиву
 2. **и при этом всё равно ложится записью** — это главный пункт
 3. обычная мысль вопросом не считается и лишнего сообщения не получает
 4. риторический вопрос внутри длинной мысли — не вопрос к архиву
 5. упавший поиск не съедает запись и объясняется вслух
 6. приватный режим не отправляет запрос наружу (инвариант 5)
 7. дважды доставленный апдейт отвечает один раз
 8. `/recall <тема>` показывает **несколько** записей во времени, а не одну
 9. видно движение: записи разных месяцев разделены названием месяца
10. тема, где нечего цитировать, отвечается честно, а не «не нашёл»
11. «что там по разработке» обычным сообщением даёт тот же рассказ
12. словоформа («разработке») находит тему («разработка»)
13. ни одного вызова модели ни на одном из путей

**Пункт 2 — главный, и он про цену ошибки.** Признак «вопрос к архиву»
разбирает код по конечному списку форм и ошибается в обе стороны. Ошибка
«не узнал вопрос» стоит одного не полученного ответа; ошибка «принял мысль
за вопрос» не стоит ничего только потому, что запись сохраняется **всегда**,
а поиск показывается **дополнительно**. Если этот порядок когда-нибудь
перевернут, `entries` не поможет: таблица только на вставку, и не сохранённая
мысль не восстановится ниоткуда.

**Вход системы — сообщение боту**, а не вызов `archive_question.asked`.
Дальше идёт настоящий `Dispatcher` с настоящим whitelist'ом: подменён только
транспорт к Telegram, поиск и (в быстрых тестах) транзакция приёма.

Тексты — живые, по номерам записей (ADR-0002: архив в git не уходит). Возраст
записей отсчитывается от настоящих часов: обработчики берут `datetime.now`
сами, и прибитая дата разъехалась бы с ними на следующий день, оставшись
зелёной.
"""

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from brain.adapters.telegram import handlers, search_text
from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.present import archive_question, archive_topics
from brain.core.retrieval.search import Found
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.topic import EntryTopic, Topic, normalize
from tests import archive as archive_module
from tests.fakes import FakeUnitOfWork, ScriptedModel
from tests.telegram_harness import OWNER_ID, FakeSession, make_bot, make_message, make_update

TIMEZONE = "Europe/Rome"


# --- Быстрая часть: вопрос без команды, без базы и без сети ---


FOUND = Found(
    entry_id=25,
    created_at=datetime(2026, 8, 16, 9, 30, tzinfo=UTC),
    timezone=TIMEZONE,
    is_private=False,
    text="Так, я наконец-то взял все в работу",
    score=0.03,
    channels=("fulltext", "vector"),
)


class SearchSpy:
    """Поиск, который ничего не ищет, но помнит, о чём спросили."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.queries: list[str] = []

    async def __call__(self, embedder: object, query: str, *, limit: int = 5) -> list[Found]:
        self.queries.append(query)
        if self.fails:
            raise RuntimeError("провайдер эмбеддингов лежит")
        return [FOUND]


class LocalSearchSpy:
    """Поиск без вектора — то, чем отвечает приватный режим с 07.09.2026."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __call__(self, query: str, *, limit: int = 5) -> list[Found]:
        self.queries.append(query)
        return [FOUND]


class TopicSpy:
    """Разговор по теме, который ничего не знает: тем в быстрых тестах нет."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, name: str, now: datetime) -> archive_topics.Talk | None:
        self.asked.append(name)
        return None


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def searcher() -> SearchSpy:
    return SearchSpy()


@pytest.fixture
def local_searcher() -> LocalSearchSpy:
    return LocalSearchSpy()


@pytest.fixture
def topics() -> TopicSpy:
    return TopicSpy()


@pytest.fixture
def private() -> bool:
    """Приватный режим. Параметризуется тестом, которому он нужен."""
    return False


@pytest.fixture
def fast(
    monkeypatch: pytest.MonkeyPatch,
    uow: FakeUnitOfWork,
    searcher: SearchSpy,
    local_searcher: LocalSearchSpy,
    topics: TopicSpy,
    private: bool,
) -> Dispatcher:
    """Бот без базы: подменены транспорт, транзакция приёма, поиск и темы."""

    @asynccontextmanager
    async def fake_transaction() -> AsyncGenerator[FakeUnitOfWork]:
        yield uow

    async def private_mode() -> bool:
        return private

    monkeypatch.setattr("brain.adapters.telegram.handlers.capture_transaction", fake_transaction)
    monkeypatch.setattr("brain.adapters.telegram.handlers.repository.is_private_mode", private_mode)
    monkeypatch.setattr("brain.adapters.telegram.handlers.search.search", searcher)
    monkeypatch.setattr("brain.adapters.telegram.handlers.search.local", local_searcher)
    monkeypatch.setattr("brain.adapters.telegram.handlers.archive_topics.collect", topics)
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.embeddings.build_embedder", lambda settings: object()
    )
    return _dispatcher()


def _dispatcher(*, archive_answers: bool = True) -> Dispatcher:
    """Диспетчер на настройках, заданных явно.

    `_env_file=None` намеренно: тест не должен зависеть от `.env` разработчика.
    Поэтому и переключатели проверяются **аргументом**, а не `monkeypatch
    .setenv` — окружение сюда не доходит вовсе, и `setenv` дал бы зелёный тест,
    ничего не проверяющий.
    """
    return build_dispatcher(
        Settings(
            _env_file=None,
            telegram_bot_token="123:test-token",
            google_api_key="AI-test",
            telegram_allowed_user_ids=[OWNER_ID],
            brain_timezone=TIMEZONE,
            archive_answers=archive_answers,
        )
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


async def _say(dispatcher: Dispatcher, bot: Bot, said: str, update_id: int = 1) -> None:
    await dispatcher.feed_update(
        bot, make_update(make_message(text=said, message_id=100 + update_id), update_id=update_id)
    )


async def test_a_question_is_answered_from_the_archive(
    fast: Dispatcher, bot: Bot, session: FakeSession, searcher: SearchSpy
) -> None:
    """Ради чего всё и делалось: вопрос без команды получает ответ по архиву."""
    await _say(fast, bot, "что там по учёбе?")

    assert searcher.queries == ["учёбе"]
    assert "Нашёл по запросу" in session.texts[-1]


async def test_a_question_is_still_written_down(
    fast: Dispatcher, bot: Bot, uow: FakeUnitOfWork, session: FakeSession
) -> None:
    """Главный пункт: ответ по архиву **добавляется** к записи, а не заменяет её.

    Признак может сработать на мысли — тогда владелец получит лишнее сообщение.
    Он не может стоить записи: `entries` только на вставку, и потерянную мысль
    не вернуть ни командой, ни из логов.
    """
    await _say(fast, bot, "что там по учёбе?")

    assert [entry.raw_text for entry in uow.entries] == ["что там по учёбе?"]
    assert handlers.ACK in session.texts
    assert "Записал." in session.texts


async def test_the_receipt_comes_before_the_answer(
    fast: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Подтверждение приёма первое, ответ по архиву — отдельным сообщением.

    Порядок держит обещание бота: «Принял.» обязано прийти быстрее 300 мс,
    а ответ по архиву стоит вызова провайдера за вектором запроса.
    """
    await _say(fast, bot, "что там по учёбе?")

    assert session.texts[0] == handlers.ACK
    assert session.texts[1] == "Записал."
    assert "Нашёл по запросу" in session.texts[2]


async def test_the_switch_silences_the_answer_and_keeps_the_entry(
    fast: Dispatcher,
    bot: Bot,
    uow: FakeUnitOfWork,
    searcher: SearchSpy,
    session: FakeSession,
) -> None:
    """`ARCHIVE_ANSWERS=false` снимает механизм — и не трогает запись.

    Выключатель есть у всего, что живёт на **каждом** сообщении
    (`LINKS_ENABLED`, `CLEANUP_ENABLED`), и заведён он вместе с механизмом,
    а не после: настройка без ветки за ней — это обещание, которое некому
    выполнить, а ветка без теста от входа системы — код, который не вызывается.
    Именно так проект уже потерял `loops.is_stuck` на месяц.

    Утверждений два, и второе важнее первого: выключенный ответ по архиву
    обязан оставить запись на месте. Выключатель, который заодно перестал бы
    сохранять мысль, был бы хуже самого механизма.
    """
    # Фикстура `fast` нужна ради своих подмен — поиска, темы, транзакции;
    # диспетчер берётся свой, с выключенным механизмом.
    silent = _dispatcher(archive_answers=False)
    assert fast is not silent

    await _say(silent, bot, "что там по учёбе?")

    assert searcher.queries == []
    assert session.texts == [handlers.ACK, "Записал."]
    assert [entry.raw_text for entry in uow.entries] == ["что там по учёбе?"]


async def test_an_ordinary_thought_is_not_a_question(
    fast: Dispatcher, bot: Bot, uow: FakeUnitOfWork, searcher: SearchSpy, session: FakeSession
) -> None:
    """Мысль без признаков вопроса — просто запись, и лишнего сообщения нет."""
    await _say(fast, bot, "Сегодня разобрал бота и понял, что дальше делать с курсовой")

    assert len(uow.entries) == 1
    assert searcher.queries == []
    assert session.texts == [handlers.ACK, "Записал."]


async def test_a_broken_search_does_not_cost_the_entry(
    fast: Dispatcher, bot: Bot, uow: FakeUnitOfWork, searcher: SearchSpy, session: FakeSession
) -> None:
    """Провайдер лежит — запись всё равно на месте, и об этом сказано вслух."""
    searcher.fails = True

    await _say(fast, bot, "покажи, что я думал про курсовую")

    assert [entry.raw_text for entry in uow.entries] == ["покажи, что я думал про курсовую"]
    assert session.texts[-1] == handlers.SEARCH_FAILED


@pytest.mark.parametrize("private", [True])
async def test_a_private_question_never_leaves_the_machine(
    fast: Dispatcher,
    bot: Bot,
    uow: FakeUnitOfWork,
    searcher: SearchSpy,
    local_searcher: LocalSearchSpy,
    session: FakeSession,
) -> None:
    """Инвариант 5. Поиску нужен вектор запроса, а вектор считают снаружи.

    **Переписано 07.09.2026 по решению владельца.** До этого дня приватный
    режим не искал по архиву вовсе и объяснял почему, а команда `/find` в том
    же режиме искала и уходила наружу. Довод владельца дословно:
    *несогласованность двух дверей хуже любой из них — привыкнет к одной,
    ошибётся на другой.*

    Теперь обе двери делают одно: снимают **канал вектора**, а не поиск.
    Два канала из трёх считаются внутри Postgres и наружу не ходят.
    """
    await _say(fast, bot, "что там по учёбе?")

    assert searcher.queries == [], "за вектором пошли в приватном режиме"
    assert local_searcher.queries == ["учёбе"]
    assert search_text.WITHOUT_VECTOR in session.texts[-1]
    assert [entry.raw_text for entry in uow.entries] == ["что там по учёбе?"]


async def test_a_redelivered_update_answers_once(
    fast: Dispatcher, bot: Bot, uow: FakeUnitOfWork, searcher: SearchSpy
) -> None:
    """Тот же апдейт дважды — один ответ: владелец спрашивал один раз."""
    message = make_message(text="что там по учёбе?", message_id=101)
    await fast.feed_update(bot, make_update(message, update_id=1))
    await fast.feed_update(bot, make_update(message, update_id=1))

    assert len(uow.entries) == 1
    assert searcher.queries == ["учёбе"]


async def test_a_reminder_is_not_a_question(
    fast: Dispatcher, bot: Bot, uow: FakeUnitOfWork, searcher: SearchSpy
) -> None:
    """«напомни …» уходит в напоминания, а не в поиск.

    Проверяется здесь, а не в `test_reminders`: слово «напомни» намеренно
    не входит в список признаков (`archive_question.OPENINGS`), и это правило
    держится ровно до первого, кто решит его туда дописать.
    """
    await _say(fast, bot, "напомни мне завтра в 10 позвонить в банк")

    assert len(uow.entries) == 1
    assert searcher.queries == []


# --- Живая речь: на чём признак не имеет права срабатывать ---


LOUD = (22, 43, 45, 51, 60, 64, 68, 71, 78)
"""Живые записи с риторическим вопросом внутри: «ну, а что за зашоренность?».

Их девять из 85, и это ровно та причина, по которой вопросительный знак сам
по себе считается признаком только на коротком сообщении (`MARK_LIMIT`).
Придумать такие строки нельзя: в голове мысль звучит утверждением.
"""


@pytest.mark.parametrize("entry_id", LOUD)
async def test_a_rhetorical_question_inside_a_thought_is_not_a_question(
    entry_id: int,
    fast: Dispatcher,
    bot: Bot,
    uow: FakeUnitOfWork,
    searcher: SearchSpy,
    archive: list[archive_module.Entry],
) -> None:
    """Мысль с «?» внутри — мысль. Иначе поиск отвечал бы на каждую пятую."""
    said = archive_module.entry(archive, entry_id).text

    await _say(fast, bot, said)

    assert len(uow.entries) == 1
    assert searcher.queries == [], f"запись {entry_id} принята за вопрос к архиву"


def test_the_whole_live_archive_is_not_mistaken_for_questions(
    archive: list[archive_module.Entry],
) -> None:
    """Ни одна развёрнутая живая запись не выглядит вопросом к архиву.

    Порог здесь `MARK_LIMIT`: короткие записи архива — это «в 14:50» и «всё ли
    работает?», то есть служебные строки, а не мысли. Утверждение проверяется
    на всём архиве целиком, потому что цена ошибки — лишнее сообщение
    на каждой пятой записи.
    """
    mistaken = [
        entry.id
        for entry in archive
        if len(" ".join(entry.text.split())) > archive_question.MARK_LIMIT
        and archive_question.asked(entry.text) is not None
    ]

    assert mistaken == []


# --- Живая база: разговор по теме ---


BUILD = "разработка"
TRIFLES = "мелочи"
# Тема из одной развёрнутой, но сегодняшней записи: причина молчания здесь
# другая, и ответ обязан называть её, а не говорить «вскользь».
CALLS = "созвоны"

# Записи, разложенные по времени от настоящих часов: обработчик берёт `now`
# сам, и прибитая дата разъехалась бы с ним назавтра.
#
# Размах — 45 дней. Он выбран не «побольше»: месяц пересекается при любом
# размахе от 32 дней, и только на таком наборе видно то, ради чего разговор
# и затеян, — «в августе писал так, в сентябре так».
PLACED: list[tuple[int, str, timedelta]] = [
    (63, BUILD, timedelta(days=45)),
    (31, BUILD, timedelta(days=38)),
    (10, BUILD, timedelta(days=25)),
    (21, BUILD, timedelta(days=18)),
    (53, BUILD, timedelta(days=10)),
    (25, BUILD, timedelta(days=3)),
    # Обе короче ста символов: тема, которой владелец касался вскользь.
    (20, TRIFLES, timedelta(days=9)),
    (15, TRIFLES, timedelta(days=4)),
    # Развёрнутая (1647 символов), но принята час назад.
    (67, CALLS, timedelta(hours=1)),
]

# Единственная запись темы «созвоны». Показывать её нечего не потому, что
# коротко, а потому, что сказано сегодня.
TODAYS = 67

# Самая старая и самая свежая записи темы. Показываются всегда: между ними
# и видно движение (`archive_topics._spread`).
OLDEST = 63
NEWEST = 25


@pytest.fixture
async def archived(migrated_database: str, archive: list[archive_module.Entry]) -> None:
    """Живые тексты, разложенные по темам и по времени."""
    now = datetime.now(UTC)
    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            for entry_id, _, age in PLACED:
                source = archive_module.entry(archive, entry_id)
                await connection.execute(
                    insert(Entry).values(
                        id=entry_id,
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind(source.kind),
                        raw_text=source.text,
                        created_at=now - age,
                        is_private=False,
                        timezone=TIMEZONE,
                        dedup_key=f"archive:{entry_id}",
                        meta={},
                    )
                )
    finally:
        await engine.dispose()

    async with session_scope() as session:
        # Часть тем уже заведена миграцией базового списка (ADR-0011), поэтому
        # тема сначала ищется и только потом заводится.
        topics: dict[str, int] = {}
        for name in (BUILD, TRIFLES, CALLS):
            slug = normalize(name)
            found = await session.scalar(select(Topic.id).where(Topic.slug == slug))
            topics[name] = int(
                found
                if found is not None
                else await session.scalar(
                    insert(Topic).values(name=name, slug=slug).returning(Topic.id)
                )
                or 0
            )

        for entry_id, topic, _ in PLACED:
            annotation_id = await session.scalar(
                insert(Annotation)
                .values(
                    source_entry_id=entry_id,
                    type=AnnotationType.THOUGHT,
                    state=AnnotationState.SURE,
                    entities=[],
                    provider="test",
                    model="test",
                )
                .returning(Annotation.id)
            )
            await session.execute(
                insert(EntryTopic).values(
                    source_entry_id=entry_id,
                    topic_id=topics[topic],
                    annotation_id=annotation_id,
                )
            )


@pytest.fixture
def writer() -> ScriptedModel:
    """Модель, которую здесь не должны позвать ни разу."""
    return ScriptedModel([{"phrase": "этого никто не увидит"}])


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch, writer: ScriptedModel) -> Dispatcher:
    """Настоящий бот на живой базе: подменён только транспорт и провайдер."""
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.llm.build_model", lambda settings, model: writer
    )
    return _dispatcher()


def _head(archive: list[archive_module.Entry], entry_id: int) -> str:
    """Начало живой записи — по нему видно, попала ли она в сообщение."""
    return archive_module.entry(archive, entry_id).text[:60]


@pytest.mark.integration
async def test_a_topic_shows_several_entries_in_time(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """Не одна случайная запись, а несколько — и в порядке времени.

    До 06.09.2026 `/recall <тема>` отдавала одну случайную запись, и по ней
    не было видно ничего: ни того, что владелец думал месяц назад, ни того,
    что он думает сейчас.
    """
    await _say(live, bot, f"/recall {BUILD}")

    said = session.texts[-1]
    places = [
        said.find(_head(archive, entry_id)) for entry_id, topic, _ in PLACED if topic == BUILD
    ]
    shown = [place for place in places if place >= 0]

    assert len(shown) >= 3, "разговор по теме показал меньше трёх записей"
    assert shown == sorted(shown), "записи показаны не в порядке времени"
    assert said.find(_head(archive, OLDEST)) >= 0, "самой старой записи темы нет"
    assert said.find(_head(archive, NEWEST)) >= 0, "самой свежей записи темы нет"


@pytest.mark.integration
async def test_the_movement_is_visible_by_months(
    archived: None, live: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """«В августе писал так, в сентябре так» — заголовками месяцев.

    Названия месяцев не прибиты: набор растянут на 45 дней, а такой размах
    пересекает границу месяца в любой день года. Прибитый «Август» был бы
    зелёным до 1 сентября и врал бы после.
    """
    await _say(live, bot, f"/recall {BUILD}")

    said = session.texts[-1]
    named = [month for month in archive_topics.MONTHS if f"{month.capitalize()}:" in said]

    assert len(named) >= 2, f"движения по месяцам не видно: {said!r}"


@pytest.mark.integration
async def test_every_shown_entry_carries_its_date(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """`vision.md` §8: синтез плюс источники с датами, всегда есть куда провалиться."""
    await _say(live, bot, f"/recall {BUILD}")

    quoted = [
        line
        for line in session.texts[-1].splitlines()
        if any(_head(archive, entry_id) in line for entry_id, topic, _ in PLACED if topic == BUILD)
    ]

    assert quoted, "ни одной цитаты"
    for line in quoted:
        assert re.match(r"^\d{2}\.\d{2}(\.\d{4})? · ", line), f"строка без даты: {line!r}"


@pytest.mark.integration
async def test_a_topic_touched_in_passing_is_answered_honestly(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """«Касался дважды, оба раза вскользь» — полноценный ответ (`vision.md` §8).

    «Ничего не нашёл» на этом месте было бы враньём: записи есть, просто
    рассказывать по ним нечего. Сами обрывки при этом не цитируются — «напомни
    в 14:50 пойти домой» вспоминать не за чем.
    """
    await _say(live, bot, f"/recall {TRIFLES}")

    said = session.texts[-1]
    assert "дважды" in said
    assert "вскользь" in said
    for entry_id, topic, _ in PLACED:
        if topic == TRIFLES:
            assert _head(archive, entry_id) not in said


@pytest.mark.integration
async def test_a_topic_recorded_today_is_told_about(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """**Перевёрнуто 07.09.2026 решением владельца.**

    Прежде этот тест проверял обратное: запись темы «созвоны» развёрнутая —
    1647 символов, — но принята час назад, и разговор про неё молчал, потому
    что порог «старше суток» достался ему от всплывания.

    Владелец отменил это прямо: у всплывания порог верен — оно достаёт старое
    **без повода**, и сегодняшнее он помнит без бота. У разговора по теме
    вопрос обратный: «что там по учёбе» спрашивают про сейчас, и самая свежая
    запись важнее всех — она и есть «а сейчас».
    """
    await _say(live, bot, f"/recall {CALLS}")

    said = session.texts[-1]
    assert _head(archive, TODAYS) in said, "сегодняшняя запись не показана"
    assert "Вспоминать пока нечего" not in said


@pytest.mark.integration
async def test_a_topic_is_found_by_its_word_form(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """«разработке» — та же тема, что «разработка».

    Морфологии в проекте нет, и зависимость ради неё не заводится: сравнивается
    общее начало слов. Без этого владелец, спросивший «что там по разработке»,
    получал бы «не нашёл» по теме, которую бот сам ему и назвал.
    """
    await _say(live, bot, "/recall разработке")

    assert _head(archive, NEWEST) in session.texts[-1]


@pytest.mark.integration
async def test_an_unknown_topic_still_answers_with_the_known_ones(
    archived: None, live: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Тупик остаётся подсказкой: словоформы не должны находить что попало."""
    await _say(live, bot, "/recall квантовая механика")

    assert "квантовая механика" in session.texts[-1]


@pytest.mark.integration
async def test_a_plain_question_tells_the_same_story(
    archived: None,
    live: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """Путь целиком: обычное сообщение → рассказ по теме → и запись на месте.

    Тест на функцию не заменяет теста на путь: здесь настоящая транзакция
    приёма, настоящая база и настоящий разбор признака — ровно то, что
    происходит, когда владелец пишет боту с телефона.
    """
    await _say(live, bot, "что там по разработке?")

    said = session.texts[-1]
    assert _head(archive, OLDEST) in said
    assert _head(archive, NEWEST) in said

    async with session_scope() as query:
        stored = await query.scalar(
            select(Entry.id).where(Entry.raw_text == "что там по разработке?")
        )
    assert stored is not None, "вопрос не лёг записью"


@pytest.mark.integration
async def test_the_model_is_never_called(
    archived: None, live: Dispatcher, bot: Bot, writer: ScriptedModel
) -> None:
    """Минимальный режим Фазы 4: формулировки — код, а не двадцать запросов в сутки."""
    await _say(live, bot, f"/recall {BUILD}")
    await _say(live, bot, "что там по разработке?", update_id=2)
    await _say(live, bot, f"/recall {TRIFLES}", update_id=3)

    assert writer.calls == 0
