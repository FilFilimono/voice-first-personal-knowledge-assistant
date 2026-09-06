"""Раскачка целиком — от тика до записи, в которую лёг ответ владельца.

**Почему от `service.tick`, а не от `prompting.pick`.** Правило проекта названо
ценой: `loops.is_stuck` была написана, покрыта тестом и не звалась из `src/`
ни разу, отчего смена вопроса у петли не работала месяц. Раскачка устроена так
же — отбор, текст, отправка и разбор ответа лежат в четырёх разных модулях, —
и проверять её по кускам значило бы повторить ту же дыру десятым способом.

Цепочка здесь такая:

    тик → вопрос ушёл → владелец ответил настоящим апдейтом
        → ответ лёг записью → запись связана с тем, на что отвечали

Ответ приходит через настоящий `Dispatcher` с настоящим whitelist: разбор
`reply_to_message` делает aiogram, и проверять на своей имитации бессмысленно.

**Данные — из живой базы по номеру записи** (`tests/archive.py`, ADR-0002).
Выдуманная «похожая» строка не ломает цитирование так, как ломает живая речь.

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import insert, select
from sqlalchemy import text as sql

from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings, get_settings
from brain.core.digest import loops, prompting, service, slots, touches
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.present import guard
from brain.core.retrieval.chunking import split
from brain.core.retrieval.indexing import index_entry
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.touch import SilenceReason, TouchSlot
from tests import archive as archive_module
from tests.fakes import FakeNotifier, RecordedEmbedder, ScriptedModel
from tests.telegram_harness import CHAT_ID, FakeSession, make_bot, make_reply_update

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"

# 09:30 по Риму 20 августа 2026 — внутри утреннего окна, как в `test_digest_tick`.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)

# Живые записи. 53 — та же, что в `test_digest_no_reproach`: в ней есть и дела,
# и мысль про бота. 7 и 36 берутся как материал темы: важно, что это настоящая
# речь, а не наша выдумка про «учёбу».
DEEDS_ENTRY = 53
TOPIC_ENTRIES = (7, 36, 43)

TOPIC = "учёба"
# Как тема выглядит в базе: имена нормализуются, «ё» становится «е»
# (`storage/models/topic.normalize`). Проверяем по тому, что увидит владелец.
TOPIC_SHOWN = "учеб"

# --- Материал двух переписанных видов (решение владельца 06.09.2026) ---
#
# Пара для связанного всплывания. Числа — не оценка на глаз, а прогон кэша
# настоящих векторов (тот же файл, что у `test_links`): 63 ↔ 25 дают 0.8562
# при пороге 0.80, то есть связь находится; 63 ↔ 54 дают 0.7418, то есть
# не находится. Обе старые записи одинаково стары и одинаково длинны —
# отличаются они **только смыслом**, и в этом весь смысл вида: старое в архиве
# есть всегда, а показывается оно, только если оно про то же самое.
LOOK_BACK_NOW = 63
LOOK_BACK_NEAR = 25
LOOK_BACK_FAR = 54


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


async def _record(text: str, *, at: datetime, message_id: int, private: bool = False) -> int:
    """Положить запись с нужной датой — сырым SQL, как в `test_digest_no_reproach`.

    Через `ingest` это сделать нельзя: `created_at` ставит база, а поправить
    его нечем — `entries` только на вставку, и UPDATE отозван триггером
    (инвариант 3). Тот же приём и по той же причине уже стоит в соседнем тесте.
    Тексты при этом настоящие, из живого архива, — а именно они и решают:
    раскачка цитирует речь владельца дословно.
    """
    async with session_scope() as db:
        entry_id = await db.scalar(
            sql(
                "INSERT INTO entries "
                "(created_at, source, kind, raw_text, is_private, timezone, dedup_key, meta) "
                "VALUES (:created_at, 'telegram', 'text', :body, :private, :tz, :key, "
                " '{}'::jsonb) RETURNING id"
            ),
            {
                "created_at": at,
                "body": text,
                "private": private,
                "tz": TIMEZONE,
                "key": f"prompting:{message_id}",
            },
        )
    assert entry_id is not None
    return int(entry_id)


async def _topic_gone_quiet(entries: list[archive_module.Entry], *, days: int = 5) -> None:
    """Тема, по которой давно нет записей, — материал для главного вопроса.

    Записи настоящие, разметка тоже проходит рабочим путём (`save_annotation`),
    потому что тема живёт в `entry_topics`, а не в наших представлениях о ней.
    """
    for number in TOPIC_ENTRIES:
        body = archive_module.entry(entries, number).text
        entry_id = await _record(body, at=MORNING - timedelta(days=days), message_id=100 + number)
        await enrichment.save_annotation(
            entry_id=entry_id,
            type_=AnnotationType.THOUGHT,
            alternative_type=None,
            state=AnnotationState.SURE,
            entities=[],
            provider="test",
            model="test",
            existing_topic_ids=[],
            new_topics=[
                enrichment.NewTopic(
                    name=TOPIC, reason="тема теста", embedding=None, embedding_model=None
                )
            ],
            units=[
                MergedUnit(
                    start=0,
                    end=len(body),
                    type=AnnotationType.THOUGHT,
                    alternative_type=None,
                    state=AnnotationState.SURE,
                )
            ],
            transcript_id=None,
            text=body,
        )


async def _deeds(entries: list[archive_module.Entry]) -> int:
    """Живая запись с делами — чтобы утренняя сводка не была пустой."""
    body = archive_module.entry(entries, DEEDS_ENTRY).text
    entry_id = await _record(body, at=MORNING - timedelta(days=1), message_id=10)
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
                end=len(body),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=body,
    )
    return entry_id


def _said(notifier: FakeNotifier) -> str:
    """Всё, что бот сказал за тик, одной строкой."""
    return "\n".join(
        [text for _, text, _, _ in notifier.mornings]
        + [text for _, text in notifier.quiet]
        + [text for _, text, _ in notifier.evenings]
    )


def _morning_today() -> datetime:
    """Утро **сегодняшних** суток владельца: 09:30 по Риму, внутри окна слота.

    Не константа, и это не педантизм. Связанное всплывание решает по календарю
    владельца, сегодняшняя запись или вчерашняя, а «что мешает» тем же способом
    выбирает между «Вчера» и датой. Прибитая дата вместе с прибитыми датами
    записей проверяла бы арифметику, которую мы сами же и посчитали; дата
    от часов проверяет ту же границу на настоящих сутках, какой бы день
    ни стоял в календаре в момент прогона.

    Час при этом остаётся нашим, а не берётся у часов: `slots.due` открывает
    утреннее окно с 09:00 до 12:00, и тик, запущенный «прямо сейчас» в три часа
    ночи, не дошёл бы до сводки вовсе — тест был бы красным две трети суток
    по причине, к раскачке не относящейся.
    """
    today = datetime.now(UTC).astimezone(ZoneInfo(TIMEZONE)).date()
    return datetime.combine(today, time(9, 30), tzinfo=ZoneInfo(TIMEZONE)).astimezone(UTC)


def _date(moment: datetime) -> str:
    """Дата так, как её пишет сообщение, — в календаре владельца, не в UTC."""
    return f"{moment.astimezone(ZoneInfo(TIMEZONE)):%d.%m}"


def _quote_of(entries: list[archive_module.Entry], number: int) -> str:
    """Цитата записи архива ровно в том виде, в каком её ставит вопрос."""
    return prompting.quote(archive_module.entry(entries, number).text)


async def _annotated(
    entries: list[archive_module.Entry],
    number: int,
    *,
    at: datetime,
    message_id: int,
    type_: AnnotationType = AnnotationType.THOUGHT,
    topic: str | None = None,
) -> int:
    """Живая запись с разметкой: тип, при желании тема. Возвращает номер в базе.

    То же, что делает `_topic_gone_quiet`, но по одной записи и с выбором типа:
    «что мешает» смотрит именно на тип (`decision` и `insight` — движение),
    а связанное всплывание — на тему свежей записи и на тип старой.

    Разметка кладётся рабочим путём (`save_annotation`), а не вставкой в таблицы:
    тема живёт в `entry_topics`, тип — в `annotations`, и оба спрашиваются
    из продакшн-кода, а не из наших представлений о схеме.
    """
    body = archive_module.entry(entries, number).text
    entry_id = await _record(body, at=at, message_id=message_id)
    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=type_,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[]
        if topic is None
        else [
            enrichment.NewTopic(
                name=topic, reason="тема теста", embedding=None, embedding_model=None
            )
        ],
        units=[
            MergedUnit(
                start=0,
                end=len(body),
                type=type_,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=body,
    )
    return entry_id


async def _with_vectors(entries: list[archive_module.Entry], indexed: dict[int, int]) -> None:
    """Посчитать вектора записям — настоящие, из кэша, без единого вызова наружу.

    Ключ словаря — номер записи в живом архиве, значение — её номер в одноразовой
    базе. Заглушка с хэшом от текста тут не годится в принципе: связанное
    всплывание **и есть** близость, а у близости на хэше нет смысла — ровно
    тот же довод, по которому `test_links` берёт `RecordedEmbedder`.
    """
    vectors = await archive_module.vectors(
        documents=[
            chunk
            for number in indexed
            for chunk in split(archive_module.entry(entries, number).text)
        ],
        queries=[],
    )
    if vectors is None:
        pytest.skip(archive_module.NO_VECTORS)

    embedder = RecordedEmbedder(vectors, get_settings().embedding_model)
    for entry_id in indexed.values():
        await index_entry(embedder, entry_id=entry_id)


async def _make_private(entry_id: int) -> None:
    """«Сделать приватной» задним числом — вектор при этом уже посчитан.

    Тем же приёмом и по той же причине, что в `test_links`: спрятав запись
    до индексации, мы проверяли бы отсутствие вектора, а не условие
    `~is_private_now()` в запросе. Оно и должно поймать тот день, когда вектор
    у приватной записи всё-таки появится.
    """
    async with session_scope() as db:
        await db.execute(
            insert(EntryOverride).values(
                source_entry_id=entry_id, action=OverrideAction.MAKE_PRIVATE, payload={}
            )
        )


async def _morning_touch(now: datetime, notifier: FakeNotifier) -> tuple[str, dict[str, Any]]:
    """Тикнуть и вернуть сказанное вместе со строкой журнала за утро.

    Обе половины нужны всегда: текст показывает, что прочитает владелец,
    а нагрузка журнала — **какой вид** выбрала раскачка. По одному тексту этого
    не видно: подстроки может не быть и по случайности.
    """
    await service.tick(now=now, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None)
    recorded = (await touches.of_day(slots.local_date(now, TIMEZONE)))[TouchSlot.MORNING]
    return _said(notifier), dict(recorded.payload)


async def test_a_silent_morning_asks_instead_of_saying_nothing(
    archive: list[archive_module.Entry],
) -> None:
    """Диагноз владельца в одном тесте: раньше здесь было молчание.

    Дел нет, петли нет — сводке сказать нечего, и до раскачки слот записывал
    `nothing_to_say`. Теперь молчание превращается в повод сказать, и это
    ровно то, ради чего затеян блок A.
    """
    await _topic_gone_quiet(archive)
    notifier = FakeNotifier()

    sent = await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    assert sent == [TouchSlot.MORNING]
    assert notifier.quiet, "раскачка не ушла: слот снова промолчал"
    assert TOPIC_SHOWN in _said(notifier)

    recorded = await touches.of_day(MORNING.date())
    assert recorded[TouchSlot.MORNING].sent_at is not None
    assert recorded[TouchSlot.MORNING].payload["prompting"]["kind"] in prompting.SET


async def test_the_question_never_lands_on_top_of_another_question(
    archive: list[archive_module.Entry],
) -> None:
    """Один вопрос в сообщении — от входа системы, а не от чистой функции.

    Сводка с петлёй кончается «Сдвинулось?». Раскачка в этот день молчит:
    два вопроса подряд — ловушка допроса (разбор, §1.3).
    """
    entry_id = await _deeds(archive)
    await loops.create(entry_id=entry_id, unit_id=None, question="куда дальше с ботом")
    await _topic_gone_quiet(archive)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, text, _, _ = notifier.mornings[0]
    assert text.splitlines()[-1] == "Сдвинулось?"
    # Проверяется по журналу, а не по тексту: подстроки в сводке может не быть
    # и по случайности, а отсутствие записи в нагрузке означает, что вопрос
    # не выбирали вовсе.
    assert "prompting" not in (await touches.of_day(MORNING.date()))[TouchSlot.MORNING].payload


async def test_the_question_rides_the_summary_when_it_asks_nothing(
    archive: list[archive_module.Entry],
) -> None:
    """Сводка без петли вопросов не задаёт — значит место для раскачки есть."""
    await _deeds(archive)
    await _topic_gone_quiet(archive)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, text, _, _ = notifier.mornings[0]
    assert text.count("?") == 1
    assert TOPIC_SHOWN in text


async def test_the_answer_becomes_an_entry_tied_to_the_question(
    archive: list[archive_module.Entry],
    dispatcher: Dispatcher,
    bot: Bot,
) -> None:
    """Главное после самого вопроса: сказанное не теряется.

    Без этого раскачка даёт повод сказать и выбрасывает сказанное — то есть
    делает ровно половину дела (план Фазы 4, блок A, пункт 6).
    """
    await _topic_gone_quiet(archive)
    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    asked = await touches.of_day(MORNING.date())
    message_id = asked[TouchSlot.MORNING].payload["prompting"]["message_id"]

    await dispatcher.feed_update(
        bot,
        make_reply_update("да отпало, курс оказался слабым", reply_to_message_id=message_id),
    )

    async with session_scope() as db:
        stored = (
            await db.scalars(select(Entry).where(Entry.raw_text.like("%курс оказался%")))
        ).all()

    assert len(stored) == 1
    answered = stored[0].meta["answers"]
    assert answered["touch_id"] == asked[TouchSlot.MORNING].id
    assert answered["kind"] == asked[TouchSlot.MORNING].payload["prompting"]["kind"]
    assert answered["entry_ids"] == asked[TouchSlot.MORNING].payload["prompting"]["entry_ids"]


async def test_the_same_question_is_not_asked_two_days_running(
    archive: list[archive_module.Entry],
) -> None:
    """Молчание не повторяет вопрос, а берёт другой (решение владельца № 7).

    Про молчание при этом не говорится ни слова: упоминание «ты не ответил»
    — ретроспективный упрёк, запрещённый §7.0 п.2.
    """
    await _topic_gone_quiet(archive)
    await _deeds(archive)
    notifier = FakeNotifier()

    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )
    await service.tick(
        now=MORNING + timedelta(days=1),
        timezone=TIMEZONE,
        chat_id=CHAT_ID,
        notifier=notifier,
        model=None,
    )

    first = (await touches.of_day(MORNING.date()))[TouchSlot.MORNING]
    second = (await touches.of_day((MORNING + timedelta(days=1)).date()))[TouchSlot.MORNING]
    assert first.payload["prompting"]["kind"] != second.payload["prompting"]["kind"]

    everything = _said(notifier)
    assert "не ответил" not in everything
    assert "промолчал" not in everything


async def test_three_silences_in_a_row_make_the_question_rarer(
    archive: list[archive_module.Entry],
) -> None:
    """Частота снижается сама и молча (решение владельца № 7).

    Считается **по журналу касаний**, а не счётчиком в отдельной колонке:
    колонка, посчитавшая молчания, однажды прочиталась бы вслух — других
    судеб у числа в базе не бывает (§7.0 п.5).
    """
    await _topic_gone_quiet(archive, days=20)
    notifier = FakeNotifier()

    days = []
    for shift in range(prompting.SILENCE_STREAK + 1):
        moment = MORNING + timedelta(days=shift)
        await service.tick(
            now=moment, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
        )
        days.append((await touches.of_day(moment.date()))[TouchSlot.MORNING])

    asked = [day for day in days if day.sent_at is not None]
    assert len(asked) == prompting.SILENCE_STREAK
    assert days[-1].silence_reason is SilenceReason.NOTHING_TO_SAY


async def test_a_private_entry_is_never_quoted(
    archive: list[archive_module.Entry],
) -> None:
    """Инвариант 5 и решение владельца № 4: дословно — только неприватное.

    Приватная запись здесь — **единственный кандидат на цитату**: она старше
    двух недель и длиннее ста символов, то есть ровно то, что берёт «оглянуться
    назад». Рядом лежит затихшая тема, чтобы бот всё-таки заговорил: молчание
    по другой причине сделало бы тест зелёным само собой.
    """
    secret = archive_module.entry(archive, DEEDS_ENTRY).text
    await _record(secret, at=MORNING - timedelta(days=20), message_id=700, private=True)
    await _topic_gone_quiet(archive)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    said = _said(notifier)
    assert said, "бот промолчал — проверять нечего"
    assert secret[:40] not in said


async def test_the_written_branch_runs_when_the_flag_is_up(
    archive: list[archive_module.Entry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ветка за флагом проверяется от входа системы, с флагом, поднятым тут.

    Иначе это код, который не вызывается, — почерк `loops.is_stuck`, стоивший
    проекту месяца неработавшей смены вопроса у петли (план Фазы 4).
    """
    await _topic_gone_quiet(archive)
    monkeypatch.setenv("PROMPTING_WRITTEN", "true")
    get_settings.cache_clear()

    model = ScriptedModel([{"question": "Про учёбу три дня тишина — застряло или отпало?"}])
    notifier = FakeNotifier()

    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=model
    )

    assert model.calls == 1, "модель не позвана: ветка за флагом не вызывается"
    assert "Про учёбу три дня тишина" in _said(notifier)


async def test_the_written_question_falls_back_to_the_template_when_rejected(
    archive: list[archive_module.Entry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Гвард отверг — уходит шаблон кода, а не пустота и не вторая попытка."""
    await _topic_gone_quiet(archive)
    monkeypatch.setenv("PROMPTING_WRITTEN", "true")
    get_settings.cache_clear()

    model = ScriptedModel([{"question": "Ты так и не сел за учёбу."}])
    notifier = FakeNotifier()

    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=model
    )

    said = _said(notifier)
    assert "так и не" not in said
    assert TOPIC_SHOWN in said
    assert said.count("?") == 1


async def test_a_quote_carrying_a_reproach_never_reaches_the_owner(
    archive: list[archive_module.Entry],
) -> None:
    """Гвард стоит и на тексте, который собрал код, а не только на тексте модели.

    Цитата приезжает в вопрос **дословно**, и в собственной речи владельца
    «забыл» и «так и не» — обычные слова. В его записи это речь, в сообщении
    бота — уже упрёк, запрещённый §7.0 п.2.

    Запись берётся из живого архива поиском по стоп-слову, а не выдумывается:
    выдуманная «похожая» строка проверила бы наши представления о том, как
    выглядит упрёк. Нет такой записи — скип с объяснением, а не зелёный.
    """
    reproachful = next(
        (
            entry
            for entry in archive
            if len(entry.text) >= prompting.MIN_LENGTH
            and guard.reproach(prompting.quote(entry.text)) is not None
        ),
        None,
    )
    if reproachful is None:
        pytest.skip("в живом архиве нет записи, чья цитата содержала бы стоп-слово гварда")

    # Единственный кандидат на «оглянуться назад» — та самая запись.
    await _record(reproachful.text, at=MORNING - timedelta(days=20), message_id=800)
    await _topic_gone_quiet(archive)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    said = _said(notifier)
    assert said, "бот промолчал — проверять нечего"
    assert guard.reproach(said) is None
    assert prompting.quote(reproachful.text)[:40] not in said


# --- Связанное всплывание: старое, связанное с тем, о чём говорит владелец ---
#
# Вид переписан 6 сентября 2026 решением владельца и до этого дня не был покрыт
# ни одним тестом. Проверять его надо целиком по одной причине: он единственный
# в наборе ходит за данными **в другой модуль** (`retrieval/links`), а значит
# ломается не своим кодом, а чужими правилами — порогом близости, зазором
# по возрасту и правилом про тип цели. Тест на функцию этого не увидит.


async def test_look_back_quotes_the_old_entry_the_fresh_one_is_related_to(
    archive: list[archive_module.Entry],
) -> None:
    """Путь целиком: свежая запись с темой, старая связанная — и вопрос о ней.

    Свежая и старая записи взяты из живого архива, а близость между ними
    посчитана настоящей моделью (0.8562 при пороге 0.80). Выдуманная пара
    «похожих» строк проверила бы наши представления о похожести, а не связь.

    Проверяется и текст, и журнал. Текст — потому что читает его владелец;
    журнал — потому что подстроки в сообщении может не быть и по случайности,
    а нагрузка называет **вид** и обе записи, на которых он стоит.

    Утро тут пустое, значит вопрос и есть всё сообщение — и факт обязан ехать
    вместе с ним: «Что с тех пор изменилось?» без цитаты не значит ничего.
    Первым же прогоном 06.09.2026 это и не работало: факт лежал в отдельном
    поле (`Question.material`), а `digest/service` отдавал `asked.text`.
    """
    morning = _morning_today()
    old_at = morning - timedelta(days=30)

    fresh = await _annotated(
        archive, LOOK_BACK_NOW, at=morning - timedelta(days=1), message_id=200, topic=TOPIC
    )
    old = await _annotated(archive, LOOK_BACK_NEAR, at=old_at, message_id=201)
    await _with_vectors(archive, {LOOK_BACK_NOW: fresh, LOOK_BACK_NEAR: old})

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert payload["prompting"]["kind"] == prompting.LOOK_BACK
    # Обе записи, и в этом порядке: по ним ответ владельца связывается с тем,
    # на что он отвечал.
    assert payload["prompting"]["entry_ids"] == [fresh, old]
    assert f"Ты сейчас про тему «{TOPIC_SHOWN}" in said
    assert _date(old_at) in said
    assert _quote_of(archive, LOOK_BACK_NEAR) in said
    assert said.count("?") == 1


async def test_look_back_stays_silent_when_nothing_old_is_related(
    archive: list[archive_module.Entry],
) -> None:
    """Связного не нашлось — вид молчит, а не выдаёт случайное.

    Прямое указание владельца от 06.09.2026, и оно сильнее желания заполнить
    слот: несвязанная цитата обесценивает и все остальные.

    Старое в архиве **есть** — та же длина, тот же возраст, тот же тип, что
    у связанной записи в соседнем тесте. Отличается оно только смыслом
    (0.7418 против 0.8562 при пороге 0.80), и молчание обязано держаться
    ровно на этом. Пустой архив дал бы тот же зелёный, ничего не проверив.
    """
    morning = _morning_today()
    fresh = await _annotated(
        archive, LOOK_BACK_NOW, at=morning - timedelta(days=1), message_id=210, topic=TOPIC
    )
    far = await _annotated(archive, LOOK_BACK_FAR, at=morning - timedelta(days=30), message_id=211)
    await _with_vectors(archive, {LOOK_BACK_NOW: fresh, LOOK_BACK_FAR: far})

    said, payload = await _morning_touch(morning, FakeNotifier())

    # Молчание вида — обычный исход, а не отказ: слот говорит другим вопросом.
    assert payload["prompting"]["kind"] != prompting.LOOK_BACK
    assert "Что с тех пор изменилось" not in said
    assert _quote_of(archive, LOOK_BACK_FAR) not in said


async def test_look_back_never_points_at_an_entry_made_private(
    archive: list[archive_module.Entry],
) -> None:
    """Инвариант 5: приватная запись целью связанного всплывания не бывает.

    Приватность включена **после** индексации, то есть вектор у цели посчитан
    и лежит в базе. Механизм, полагающийся на «у приватных и так нет вектора»,
    сломался бы здесь молча — и цитата ушла бы в сообщение и в модель.

    Связанная цель здесь одна, поэтому вид замолкает целиком; говорит слот
    другим вопросом, и это делает проверку осмысленной: молчание бота
    по любой причине сделало бы тест зелёным само собой.
    """
    morning = _morning_today()
    fresh = await _annotated(
        archive, LOOK_BACK_NOW, at=morning - timedelta(days=1), message_id=220, topic=TOPIC
    )
    old = await _annotated(archive, LOOK_BACK_NEAR, at=morning - timedelta(days=30), message_id=221)
    await _with_vectors(archive, {LOOK_BACK_NOW: fresh, LOOK_BACK_NEAR: old})
    await _make_private(old)

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert said, "бот промолчал — проверять нечего"
    assert payload["prompting"]["kind"] != prompting.LOOK_BACK
    assert _quote_of(archive, LOOK_BACK_NEAR) not in said


async def test_look_back_says_now_only_about_today_and_yesterday(
    archive: list[archive_module.Entry],
) -> None:
    """Сегодняшняя запись — это «сейчас», и так и говорится.

    Граница проходит по календарю владельца, поэтому даты записей считаются
    от настоящих часов: прибитое «сегодня» перестало бы быть сегодняшним
    на следующий день после написания теста.
    """
    morning = _morning_today()
    fresh = await _annotated(
        archive, LOOK_BACK_NOW, at=morning - timedelta(hours=2), message_id=230, topic=TOPIC
    )
    old = await _annotated(archive, LOOK_BACK_NEAR, at=morning - timedelta(days=30), message_id=231)
    await _with_vectors(archive, {LOOK_BACK_NOW: fresh, LOOK_BACK_NEAR: old})

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert payload["prompting"]["kind"] == prompting.LOOK_BACK
    assert f"Ты сейчас про тему «{TOPIC_SHOWN}" in said


async def test_look_back_names_the_date_when_the_fresh_entry_is_not_fresh(
    archive: list[archive_module.Entry],
) -> None:
    """Запись четырёхдневной давности «сейчас» не называется.

    Это защита от выдуманного факта, а не от неловкой формулировки: сказать
    «ты сейчас про разработку» про позапрошлую среду значит соврать, а факт
    в этом наборе несущая часть, а не подводка. Живая проверка 06.09.2026
    нашла ровно такой случай — последняя запись архива была пятидневной.

    Запись при этом остаётся в окне `RECENT` (неделя), то есть вид срабатывает
    и говорит — просто говорит правду.
    """
    morning = _morning_today()
    fresh_at = morning - timedelta(days=4)

    fresh = await _annotated(archive, LOOK_BACK_NOW, at=fresh_at, message_id=240, topic=TOPIC)
    old = await _annotated(archive, LOOK_BACK_NEAR, at=morning - timedelta(days=30), message_id=241)
    await _with_vectors(archive, {LOOK_BACK_NOW: fresh, LOOK_BACK_NEAR: old})

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert payload["prompting"]["kind"] == prompting.LOOK_BACK
    assert f"{_date(fresh_at)} у тебя была запись по теме «{TOPIC_SHOWN}" in said
    # Проверяется фраза бота, а не слово: «сейчас» встречается и в самой цитате
    # владельца — это его речь, и трогать её никто не собирается.
    assert "Ты сейчас про" not in said


# --- «Что мешает»: тема, в которую владелец возвращается без движения ---
#
# Второй переписанный вид. Условие срабатывания у него считается кодом целиком
# и целиком невидимо в тексте сообщения: сколько записей по теме и какого они
# типа, владелец не увидит никогда (`vision.md` §7.0, п.5 — счётчики вслух
# не читаются). Значит, единственный способ проверить порог — данные.


async def test_blocker_quotes_the_last_entry_of_a_topic_that_has_not_moved(
    archive: list[archive_module.Entry],
) -> None:
    """Тема с тремя записями и без решений — факт из данных, потом вопрос.

    Записи положены позавчера намеренно: так молчат все виды, стоящие в наборе
    раньше. Затихшая тема ещё не затихла (двое суток из трёх), «вчера ты
    записал» не находит вчерашнего, возвращение не набирает трёх дней подряд,
    а связанное всплывание молчит без векторов. Остаётся один вид — и тест
    проверяет его, а не очередь.
    """
    morning = _morning_today()
    at = morning - timedelta(days=2)
    ids = [
        await _annotated(
            archive, number, at=at - timedelta(hours=shift), message_id=300 + number, topic=TOPIC
        )
        for shift, number in enumerate(TOPIC_ENTRIES)
    ]

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert payload["prompting"]["kind"] == prompting.BLOCKER
    # Цитируется последняя запись темы — она и есть факт, с которого начинается
    # вопрос. `TOPIC_ENTRIES[0]` положена позже остальных.
    assert payload["prompting"]["entry_ids"] == [ids[0]]
    assert _quote_of(archive, TOPIC_ENTRIES[0]) in said
    assert "Что мешает?" in said
    assert _date(at) in said
    assert "Вчера" not in said


@pytest.mark.parametrize("moved", [AnnotationType.DECISION, AnnotationType.INSIGHT])
async def test_blocker_stays_silent_when_the_topic_has_moved(
    archive: list[archive_module.Entry], moved: AnnotationType
) -> None:
    """Решение или понимание по теме — движение есть, спрашивать не о чем.

    Расклад тот же, что в соседнем тесте, и отличается **одним типом одной
    записи**. Соседний тест доказывает, что на этих данных вопрос приходит;
    значит, молчание здесь — следствие типа, а не пустого архива.

    Двигающая запись — не та, которую бы процитировали: движение считается
    по всей теме, а не по последней записи.
    """
    morning = _morning_today()
    at = morning - timedelta(days=2)
    for shift, number in enumerate(TOPIC_ENTRIES):
        await _annotated(
            archive,
            number,
            at=at - timedelta(hours=shift),
            message_id=310 + number,
            topic=TOPIC,
            type_=moved if number == TOPIC_ENTRIES[1] else AnnotationType.THOUGHT,
        )

    notifier = FakeNotifier()
    said, payload = await _morning_touch(morning, notifier)

    assert "prompting" not in payload
    assert said == ""


async def test_blocker_stays_silent_below_three_entries_on_the_topic(
    archive: list[archive_module.Entry],
) -> None:
    """Две записи — это ещё не «возвращается к одному и тому же».

    Порог тот же, что у затихшей темы, и по той же причине: одна-две записи —
    совпадение слов, а не тема, и вопрос про такую звучал бы выдумкой.

    Двойка здесь написана числом, а не выражена через `MIN_TOPIC_ENTRIES`
    нарочно: порог, посчитанный из той же константы, что и проверяемый код,
    съезжает вместе с ней — и тест остаётся зелёным при любом её значении,
    то есть не проверяет порог вовсе.
    """
    morning = _morning_today()
    at = morning - timedelta(days=2)
    for shift, number in enumerate(TOPIC_ENTRIES[:2]):
        await _annotated(
            archive, number, at=at - timedelta(hours=shift), message_id=320 + number, topic=TOPIC
        )

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert "prompting" not in payload
    assert said == ""


async def test_blocker_calls_yesterday_yesterday(
    archive: list[archive_module.Entry],
) -> None:
    """Вчерашнее называется вчерашним, а не датой.

    «05.09 ты писал» про вчера читается канцелярски, а вопрос держится
    на том, что владелец узнаёт в цитате себя.

    Вчерашняя запись поднимает и вид «вчера ты записал», который стоит
    в наборе раньше. Он отодвигается тем же способом, каким его отодвинул бы
    прод, — строкой в журнале касаний: выбирается не первый подходящий вопрос,
    а тот, которого дольше всех не было. Подмены здесь нет, это рабочий путь.
    """
    morning = _morning_today()
    yesterday_at = morning - timedelta(days=1)
    for number, days_ago in zip(TOPIC_ENTRIES, (1, 2, 5), strict=True):
        await _annotated(
            archive,
            number,
            at=morning - timedelta(days=days_ago),
            message_id=330 + number,
            topic=TOPIC,
        )
    await touches.record_sent(
        TouchSlot.DAY,
        slots.local_date(morning, TIMEZONE) - timedelta(days=1),
        payload={"prompting": {"kind": prompting.YESTERDAY}},
    )

    said, payload = await _morning_touch(morning, FakeNotifier())

    assert payload["prompting"]["kind"] == prompting.BLOCKER
    assert f"Вчера ты писал: «{_quote_of(archive, TOPIC_ENTRIES[0])}»" in said
    assert _date(yesterday_at) not in said
