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

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy import select
from sqlalchemy import text as sql

from brain.adapters.telegram.bot import build_dispatcher
from brain.config import Settings
from brain.core.digest import loops, prompting, service, touches
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.present import guard
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.touch import SilenceReason, TouchSlot
from tests import archive as archive_module
from tests.fakes import FakeNotifier, ScriptedModel
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
    monkeypatch.setattr(prompting, "WRITTEN", True)

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
    monkeypatch.setattr(prompting, "WRITTEN", True)

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
