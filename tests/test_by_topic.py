"""Срез по темам и «вспомним подробно» — `vision.md` §5.4, вторая половина.

Что должно работать:

1. срез называет темы периода и **не выдаёт ни одной конкретной записи**
2. тема вне периода в срез не попадает
3. тем меньше двух — молчание, а не срез из одной строки
4. `/recall <тема>` достаёт запись, и только после того, как тему назвали
5. тема ищется по нормализованному имени: «учеба» находит «учёбу»
6. слишком свежая и слишком короткая записи «вспоминанию» не подлежат
7. названа тема, которой нет, — в ответе список тем, а не «не нашёл»
8. `RECALL_WRITTEN=false` — модель не зовётся ни разу
9. `RECALL_WRITTEN=true` — первую строку пишет модель, список тем остаётся кодом
10. ретроспективного упрёка в текстах возврата нет (`vision.md` §7.0, принцип 2)

**Пункт 1 — главный.** Он и есть то, чем срез отличается от всплывания (§5.3):
выбор всегда к месту, а случайная запись полугодовой давности к месту может
не быть. Срез называет темы, конкретную запись достаёт уже вторая команда.

**Вход системы — команда `/recall`**, а не `by_topic.collect`. Дальше идёт
настоящий `Dispatcher` с настоящим whitelist'ом: подменён только транспорт
к Telegram и провайдер модели.

Тексты — живые, по номерам записей (ADR-0002: архив в git не уходит). Время
записи выбирает тест: проверяются здесь как раз пороги по времени, и брать их
из архива значило бы привязать зелёный цвет к сегодняшней дате.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from brain.adapters.telegram import handlers
from brain.adapters.telegram.bot import COMMANDS, build_dispatcher
from brain.config import Settings, get_settings
from brain.core.digest import by_topic
from brain.core.present import guard, recall
from brain.core.retrieval.links import Link
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.topic import EntryTopic, Topic, normalize
from tests import archive as archive_module
from tests.fakes import ScriptedModel
from tests.telegram_harness import OWNER_ID, FakeSession, make_bot, make_message, make_update

TIMEZONE = "Europe/Rome"

# Момент для тестов текста — фиксированный: они про слова, а не про сроки.
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

# **Возраст записей отсчитывается от настоящего «сейчас», а не от константы.**
# Команду `/recall` обрабатывает бот, и момент он берёт свой — `datetime.now`.
# Прибитая дата разъехалась бы с ним на следующий день после написания теста,
# и порог периода перестал бы что-либо значить, оставшись зелёным.
#
# Внутри недельного окна и старше суток: годится и в срез, и в «вспомним».
RECENT = timedelta(days=2)
# Вне недельного окна: тема этой записи в срезе появиться не должна.
LONG_AGO = timedelta(days=30)
# Сегодняшняя: в срез попадает, а «вспоминать» её нечего — владелец её помнит.
TODAY = timedelta(hours=1)

# Имена — как их называет базовый список тем (миграция `bd53a2b6db97`).
# «учеба» там без «ё», и это не опечатка: темы сравниваются по slug,
# а показывается имя из базы. Владелец же набирает команду как ему удобно —
# отсюда отдельный тест на «учёбу» через «ё».
DEVELOPMENT = "разработка"
STUDY = "учеба"
FILMS = "фильмы"
# Темы из одной записи каждая. Одна запись на тему — не бедность набора,
# а условие проверки: «вспомним подробно» выбирает **случайную** запись темы,
# и на теме из двух записей отказ от негодной был бы виден через раз.
ONLY_FRESH = "созвоны"
ONLY_SHORT = "мелочи"

# Живые записи и то, чем каждая тут работает. Номера, а не тексты: текст лежит
# в базе (`tests/archive.py`).
LONG_ONE = 63  # 785 символов
ANOTHER_LONG = 31  # 1958 символов
RECALLABLE = 25  # 523 символа — единственная запись своей темы
TOO_FRESH = 67  # 1647 символов, принята час назад
TOO_SHORT = 20  # 27 символов — короче ста, всплыванию не подлежит
OUT_OF_PERIOD = 53  # 495 символов, месяц назад

PLACED: list[tuple[int, str, timedelta]] = [
    (LONG_ONE, DEVELOPMENT, RECENT),
    (ANOTHER_LONG, DEVELOPMENT, RECENT),
    (RECALLABLE, STUDY, RECENT),
    (TOO_FRESH, ONLY_FRESH, TODAY),
    (TOO_SHORT, ONLY_SHORT, RECENT),
    (OUT_OF_PERIOD, FILMS, LONG_AGO),
]


# --- Тексты возврата. Без базы и без сети ---


def _link(entry_id: int = 7) -> Link:
    return Link(
        entry_id=entry_id,
        text="Надо будет дочитать книгу и разобраться с ботом",
        created_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        timezone=TIMEZONE,
        similarity=0.86,
    )


def test_the_link_template_carries_no_reproach() -> None:
    """§7.0, принцип 2. Связь возвращает к мысли, а не считает промахи.

    Проверяется тем же детектором, которым проверяется написанное моделью:
    правило одно, и второй его копии в проекте быть не должно.
    """
    assert guard.reproach(recall.template([_link()], timezone=TIMEZONE)) is None


def test_the_link_headline_promises_nothing_and_asks_nothing() -> None:
    """«Сообщение даёт, а не требует» (§7.0, принцип 1).

    Ни вопросительного знака, ни повелительного «вернись»: захочет ответить —
    ответит, ответ на сообщение бота в этой системе и есть способ что-то сказать.
    """
    assert "?" not in recall.HEADLINE
    assert guard.reproach(recall.HEADLINE) is None


async def test_the_topics_slice_carries_no_reproach() -> None:
    """Срез перечисляет темы, а не то, к чему владелец не возвращался."""
    said = await recall.topics_text(
        None,
        headline="За неделю вы возвращались к этим темам.",
        listing="разработка (3), учёба (1)",
        invitation="Вспомнить какую-то подробно: /recall разработка",
        now=NOW,
    )

    assert guard.reproach(said) is None


def test_a_week_is_called_a_week() -> None:
    """«За 7 дн.» — это счётчик в человеческом сообщении, а не срок."""
    assert "неделю" in by_topic._period(7)


# --- Живая база: срез и «вспомним подробно» ---


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
        # тема сначала ищется и только потом заводится. Слепая вставка здесь
        # падала бы на `uq_topics_slug` — и падала бы не всегда, а только
        # у тех тем, которые владелец успел завести в базовый список.
        topics: dict[str, int] = {}
        for name in (DEVELOPMENT, STUDY, FILMS, ONLY_FRESH, ONLY_SHORT):
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


# Фраза, которую вернёт подменённая модель. Отдельной фикстурой, чтобы тест
# на отказ гварда подставлял свою параметризацией, а не лез в потроха заглушки.
PHRASE = "Неделя ушла на разбор бота и учёбу"


@pytest.fixture
def phrase() -> str:
    return PHRASE


@pytest.fixture
def writer(phrase: str) -> ScriptedModel:
    """Модель, которая пишет фразу и считает, сколько раз её позвали."""
    return ScriptedModel([{"phrase": phrase}])


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch, writer: ScriptedModel) -> Dispatcher:
    """Настоящий бот: подменены только транспорт к Telegram и провайдер модели."""
    monkeypatch.setattr(
        "brain.adapters.telegram.handlers.llm.build_model", lambda settings, model: writer
    )
    return build_dispatcher(
        Settings(
            _env_file=None,
            telegram_bot_token="123:test-token",
            google_api_key="AI-test",
            telegram_allowed_user_ids=[OWNER_ID],
            brain_timezone=TIMEZONE,
        )
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return make_bot(session)


async def _ask(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    await dispatcher.feed_update(bot, make_update(make_message(text=text)))


def _text_of(archive: list[archive_module.Entry], entry_id: int) -> str:
    """Начало живой записи — по нему видно, попала ли она в сообщение."""
    return archive_module.entry(archive, entry_id).text[:60]


@pytest.mark.integration
async def test_the_slice_names_topics(
    archived: None, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    await _ask(dispatcher, bot, "/recall")

    said = session.texts[-1]
    assert DEVELOPMENT in said
    assert STUDY in said


@pytest.mark.integration
async def test_the_slice_hands_out_no_entry_without_being_asked(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """Главное правило среза (§5.4): конкретную запись без спроса не выдавать.

    Тем он и отличается от всплывания старого, которое приходит само и без
    повода. Выбор всегда к месту; случайная запись может быть не к месту.
    """
    await _ask(dispatcher, bot, "/recall")

    said = session.texts[-1]
    for entry_id, _, _ in PLACED:
        assert _text_of(archive, entry_id) not in said, f"срез выдал запись {entry_id}"


@pytest.mark.integration
async def test_the_slice_invites_to_ask(
    archived: None, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Приглашение называет команду с примером: гадать имя темы не надо."""
    await _ask(dispatcher, bot, "/recall")

    assert "/recall " in session.texts[-1]


@pytest.mark.integration
async def test_a_topic_outside_the_period_is_not_in_the_slice(
    archived: None, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Срез — про неделю, а не про архив: месячная давность в него не входит."""
    await _ask(dispatcher, bot, "/recall")

    assert FILMS not in session.texts[-1]


@pytest.mark.integration
async def test_one_topic_is_not_a_slice(
    migrated_database: str, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Молчание — обычный исход, а не поломка, и оно себя объясняет.

    База здесь пустая нарочно: «ты думал о теме „разработка“» — это не срез,
    а пересказ единственной строки.
    """
    await _ask(dispatcher, bot, "/recall")

    assert session.texts[-1] == handlers.NO_TOPICS


@pytest.mark.integration
async def test_a_named_topic_brings_one_entry(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """Названа тема — приходит запись. Это и есть «спрос», после которого можно."""
    await _ask(dispatcher, bot, f"/recall {DEVELOPMENT}")

    said = session.texts[-1]
    assert _text_of(archive, LONG_ONE) in said or _text_of(archive, ANOTHER_LONG) in said


@pytest.mark.integration
async def test_a_topic_is_found_by_its_normalized_name(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """«учеба» и «учёба» — одна тема, и регистр тоже не имеет значения.

    В базе тема названа без «ё» — так её завёл базовый список; владелец набирает
    её с «ё» и с большой буквы, как пишется. Тема ищется по slug именно поэтому:
    без нормализации команда молча не находила бы то, что бот только что сам
    и предложил.
    """
    await _ask(dispatcher, bot, "/recall Учёба")

    assert _text_of(archive, RECALLABLE) in session.texts[-1]


@pytest.mark.integration
async def test_todays_entry_is_not_something_to_recall(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """Сегодняшнее владелец помнит и без бота — та же причина, что у связей.

    В теме одна запись, и она принята час назад. Значит, вспоминать нечего,
    и ответ — список тем, а не эта запись.
    """
    await _ask(dispatcher, bot, f"/recall {ONLY_FRESH}")

    assert _text_of(archive, TOO_FRESH) not in session.texts[-1]
    assert ONLY_FRESH in session.texts[-1]


@pytest.mark.integration
async def test_a_short_entry_is_not_something_to_recall(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    archive: list[archive_module.Entry],
) -> None:
    """«напомни в 14:50 пойти домой» — не мысль, к которой стоит возвращаться.

    Порог тот же, что у всплывания (§5.3): сто символов. Правило общее нарочно
    — «что имеет смысл вспоминать» не должно решаться двумя разными числами
    в двух модулях.
    """
    await _ask(dispatcher, bot, f"/recall {ONLY_SHORT}")

    assert _text_of(archive, TOO_SHORT) not in session.texts[-1]


@pytest.mark.integration
async def test_an_unknown_topic_answers_with_the_known_ones(
    archived: None, dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    """Список вместо «не нашёл» превращает тупик в подсказку."""
    await _ask(dispatcher, bot, "/recall квантовая механика")

    said = session.texts[-1]
    assert "квантовая механика" in said
    assert DEVELOPMENT in said


# --- Кто пишет первую строку ---


@pytest.mark.integration
async def test_the_model_is_not_called_while_recall_written_is_off(
    archived: None, dispatcher: Dispatcher, bot: Bot, session: FakeSession, writer: ScriptedModel
) -> None:
    """Умолчание — шаблон кода: срез не стоит запроса из двадцати суточных."""
    await _ask(dispatcher, bot, "/recall")

    assert writer.calls == 0
    assert "За неделю" in session.texts[-1]


@pytest.mark.integration
async def test_the_model_writes_the_lead_when_the_flag_is_on(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    writer: ScriptedModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ветка за флагом обязана работать, иначе это код, который не вызывается."""
    monkeypatch.setenv("RECALL_WRITTEN", "true")
    get_settings.cache_clear()

    await _ask(dispatcher, bot, "/recall")

    said = session.texts[-1]
    assert writer.calls == 1
    assert PHRASE in said
    # Список тем и приглашение остаются кодом при любом раскладе.
    assert DEVELOPMENT in said
    assert "/recall " in said


@pytest.mark.integration
@pytest.mark.parametrize("phrase", ["вы так и не вернулись к учёбе"])
async def test_a_refused_phrase_falls_back_to_the_template(
    archived: None,
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    writer: ScriptedModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Гвард отверг — уходит прежний сухой срез, без второй попытки."""
    monkeypatch.setenv("RECALL_WRITTEN", "true")
    get_settings.cache_clear()

    await _ask(dispatcher, bot, "/recall")

    said = session.texts[-1]
    assert "так и не вернулись" not in said
    assert "За неделю" in said


# --- Обнаружаемость ---


def test_recall_is_advertised() -> None:
    """Команда, которой нет в подсказке, для владельца не существует (§14)."""
    assert any(command.command == "recall" for command in COMMANDS)
