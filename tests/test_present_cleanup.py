"""Чистка расшифровки: то, что владелец видит вместо собственных «э» и «мм».

Проверяется **наш код вокруг вызова**, а не сама модель: живой вызов меряется
`just eval-cleanup`, и это работа владельца — из сессии агента провайдер
недоступен (гео-блокировка). Здесь модель заменена сценарной заглушкой,
и это честно ровно потому, что все утверждения файла — про путь, а не про
качество текста.

Путь проверяется целиком: голосовое → расшифровка → задание чистки →
вычищенный текст в базе → перерисованное подтверждение приёма. Тест на одну
функцию его не заменяет — ревизия Задачи 1 нашла ровно это: четыре теста
дёргали выражение напрямую и не поймали бы подмену в одиннадцати местах.

Тексты — живые, по номерам записей (ADR-0002: архив в git не уходит).
Требует поднятого Postgres для всего, кроме промпта и разбора ответа.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, text

from brain.core import ingest, present, quota, status
from brain.core.enrich import repository as enrich_repository
from brain.core.memory import overrides, repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.present import cleanup
from brain.core.scheduler import jobs
from brain.core.scheduler.queue import CLEANUP, SHOW_TRANSCRIPT
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType

# Модель записи — под своим именем: `Entry` в этом файле занят живым архивом,
# и путать строку базы с текстом из архива нельзя.
from brain.storage.models.entry import Entry as EntryRow
from brain.storage.models.entry import EntryKind, EntrySource
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.transcript import Transcript
from tests.archive import Entry, entry
from tests.fakes import FakeAudioSource, FakeNotifier, ScriptedModel, StubTranscriber

TIMEZONE = "Europe/Rome"
CHAT_ID = 111
ACK_MESSAGE_ID = 21

# Живые записи по номерам. Каждая выбрана за то, что ломается по-своему.
#
# 46 — обычная мысль с «э» посередине и без просьбы: на ней проверяется
#      сам путь чистки, ничем не осложнённый.
# 26 — «чтобы ты меня напомнил, э, уси с офиса, э-э, в 7:40 вечера»:
#      расшифровка показывается **вместе со строкой напоминания**, и голая
#      подмена текста стёрла бы её.
# 27 — «Помни не уйти в 7:40 вечера»: слова «напомни» нет, система спрашивает
#      кнопками. Голая подмена стёрла бы и вопрос, и единственный способ
#      на него ответить.
PLAIN = 46
WITH_REMINDER = 26
WITH_QUESTION = 27


# --- Граница чистки: промпт и разбор ответа. Без базы и без сети ---


def test_prompt_forbids_improving_before_it_allows_cleaning() -> None:
    """«Лучше недочистить» стоит в промпте раньше списка того, что убирать.

    Порядок не косметический: модель по умолчанию склонна улучшать текст,
    и правило, стоящее после списка задач, читается как оговорка.
    """
    body = cleanup.PROMPT.lower()

    assert "лучше недочистить" in body
    assert body.index("лучше недочистить") < body.index("слова-паразиты")
    for forbidden in ("не перефразируй", "не меняй порядок", "не исправляй согласования"):
        assert forbidden in body


def test_parse_takes_clean_field() -> None:
    parsed = cleanup.parse({"items": [{"id": 7, "clean": "Надо посмотреть фильм."}]}, entry_id=7)

    assert parsed == "Надо посмотреть фильм."


def test_parse_refuses_foreign_id() -> None:
    """Модель вернула чужую запись — берём ничего, а не чужой текст.

    Та же защита, что в разметке: в пачке идентификаторы идут подряд,
    и ошибиться на единицу легко.
    """
    assert cleanup.parse({"items": [{"id": 8, "clean": "чужое"}]}, entry_id=7) is None


def test_parse_refuses_empty() -> None:
    """Пустая чистка — отказ, а не успех: показывать было бы нечего."""
    assert cleanup.parse({"items": [{"id": 7, "clean": "   "}]}, entry_id=7) is None


def test_parse_survives_an_answer_without_items() -> None:
    """Ответ не по схеме — отказ, а не исключение: чистка не стоит записи."""
    assert cleanup.parse({}, entry_id=7) is None


# --- Путь целиком ---

pytestmark_integration = pytest.mark.integration


@pytest.fixture
async def database(migrated_database: str) -> AsyncGenerator[str]:
    yield migrated_database


async def _capture_voice(audio_dir: Path, *, message_id: int = 20) -> int:
    """Принять голосовое так же, как это делает бот."""
    async with capture_transaction() as uow:
        result = await ingest.capture_voice(
            uow,
            ingest.IncomingVoice(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=ACK_MESSAGE_ID,
                file_id="file-abc",
                duration_sec=12,
            ),
            is_private=False,
            timezone=TIMEZONE,
            audio_dir=audio_dir,
        )
    assert result.entry_id is not None
    return result.entry_id


async def _transcribe(entry_id: int, spoken: str, notifier: FakeNotifier) -> None:
    """Отработать задание расшифровки так же, как это делает воркер."""
    await jobs.transcribe(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=ACK_MESSAGE_ID,
        transcriber=StubTranscriber(spoken),
        audio_source=FakeAudioSource(),
        notifier=notifier,
        max_duration_sec=900,
    )


def _model(entry_id: int, clean: str) -> ScriptedModel:
    """Модель чистки, отвечающая заданным текстом и считающая вызовы."""
    return ScriptedModel([{"items": [{"id": entry_id, "clean": clean}]}])


async def _clean(entry_id: int, model: ScriptedModel, notifier: FakeNotifier) -> None:
    await jobs.cleanup(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=ACK_MESSAGE_ID,
        model=model,
        notifier=notifier,
    )


async def _jobs_of(entrypoint: str) -> list[Any]:
    """Задания очереди по имени — со временем, начиная с которого их возьмут."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text("SELECT id, execute_after FROM pgqueuer WHERE entrypoint = :name ORDER BY id"),
                {"name": entrypoint},
            )
        ).all()
    return list(rows)


async def _refused(*, status: int, at: datetime, tier: str = "cheap") -> None:
    """Отказ провайдера, записанный в журнал вызовов."""
    async with session_scope() as session:
        await session.execute(
            insert(LlmCall).values(
                created_at=at,
                provider="gemini",
                model="gemini-3.5-flash-lite",
                tier=tier,
                purpose="enrich",
                succeeded=False,
                status_code=status,
                error="429 quota",
            )
        )


@pytest.mark.integration
async def test_cleanup_job_stores_clean_text_and_edits_message(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Голосовое проходит путь до вычищенного текста в подтверждении приёма."""
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)

    model = _model(entry_id, "Ещё есть незакрытые вопросы по моей работе в Navis.")
    await _clean(entry_id, model, notifier)

    stored = await repository.transcript_to_clean(entry_id)
    assert stored is not None
    assert stored.clean_text == "Ещё есть незакрытые вопросы по моей работе в Navis."
    assert stored.clean_model == model.model
    assert notifier.edited[-1][2] == stored.clean_text


@pytest.mark.integration
async def test_private_entry_is_never_cleaned(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Приватность включена задним числом — чистка не идёт наружу (инвариант 5)."""
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)

    await overrides.make_private(entry_id)
    model = _model(entry_id, "не должно уйти")
    await _clean(entry_id, model, notifier)

    assert model.calls == 0
    stored = await repository.transcript_to_clean(entry_id)
    assert stored is not None and stored.clean_text is None


@pytest.mark.integration
async def test_cleanup_keeps_the_reminder_line(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Перерисовка не имеет права стереть «⏰ Напомню».

    Расшифровка показывается вместе с судьбой напоминания, и голый
    `notifier.edit(chat_id, ack_message_id, clean_text)` убрал бы строку.
    Владелец прочитал бы это как «напоминание не завелось» — при заведённом
    напоминании.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, WITH_REMINDER).text, notifier)

    await _clean(entry_id, _model(entry_id, "Напомни мне уйти с офиса в 7:40 вечера."), notifier)

    _, _, shown = notifier.edited[-1]
    assert "Напомни мне уйти с офиса" in shown

    # Утверждается **само время**, а не присутствие слова «Напомню».
    # Строка собирается заново, и единственное, что отделяет «в 19:40»
    # от «в 17:40», — перевод в таймзону записи: в базе `fire_at` лежит в UTC,
    # а Europe/Rome в августе на два часа впереди. Проверка на подстроку
    # «Напомню» это молча пропустила бы.
    reminder = await repository.reminder_of(entry_id)
    assert reminder is not None, "напоминание не завелось — тест проверяет не то"
    due = reminder.fire_at.astimezone(ZoneInfo(TIMEZONE))
    assert f"⏰ Напомню {due:%d.%m в %H:%M}." in shown
    assert "в 19:40." in shown, "«в 7:40 вечера» — это 19:40 у владельца, а не в UTC"


@pytest.mark.integration
async def test_cleanup_keeps_the_question_and_its_buttons(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Запись без слова «напомни» — это вопрос с кнопками, а не текст.

    Перерисовка обычным `edit` убрала бы и вопрос, и кнопки: ответить
    на пропавший вопрос нечем.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, WITH_QUESTION).text, notifier)

    await _clean(entry_id, _model(entry_id, "Помни, не уйти в 7:40 вечера."), notifier)

    assert notifier.asked, "вопрос с кнопками пропал после чистки"
    _, _, shown, asked_about = notifier.asked[-1]
    assert asked_about == entry_id
    assert "Похоже на напоминание" in shown


@pytest.mark.integration
async def test_cleanup_keeps_the_annotation_line(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Разметка успела первой — перерисовка обязана идти её путём.

    Разметка рисует то же самое сообщение по-своему: с типом, темами
    и кнопками «не туда» и «сделать приватной». Перерисуй чистка его своим
    путём, разбор исчез бы вместе с кнопками — та же беда, что со строкой
    напоминания, только с другой стороны.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    spoken = entry(archive, PLAIN).text
    await _transcribe(entry_id, spoken, notifier)

    await enrich_repository.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.THOUGHT,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        new_topics=[],
        units=[],
        transcript_id=None,
        text=spoken,
    )
    await _clean(entry_id, _model(entry_id, "Ещё есть незакрытые вопросы по работе."), notifier)

    assert notifier.annotated, "разбор стёрт перерисовкой"
    _, _, shown, _ = notifier.annotated[-1]
    assert "Ещё есть незакрытые вопросы по работе." in shown
    assert "мысль" in shown


async def _old_voice_with_transcript(spoken: str, *, age: timedelta) -> int:
    """Голосовое, принятое давно, вместе с расшифровкой и без чистки.

    Вставкой напрямую, а не приёмом: `entries` только на вставку, и состарить
    уже созданную запись UPDATE'ом нельзя — это запрещено триггером
    (инвариант 3). Так выглядит запись, чистка которой пролежала под квотой
    до следующего дня.
    """
    async with session_scope() as session:
        entry_id = await session.scalar(
            insert(EntryRow)
            .values(
                created_at=datetime.now(UTC) - age,
                source=EntrySource.TELEGRAM,
                kind=EntryKind.VOICE,
                audio_path="var/audio/old.oga",
                is_private=False,
                timezone=TIMEZONE,
                dedup_key=f"old-{age}",
                meta={"chat_id": CHAT_ID, "ack_message_id": ACK_MESSAGE_ID},
            )
            .returning(EntryRow.id)
        )
        await session.execute(
            insert(Transcript).values(
                source_entry_id=entry_id,
                text=spoken,
                provider="stub",
                model="stub",
                latency_ms=1,
            )
        )
    return int(entry_id or 0)


@pytest.mark.integration
async def test_late_cleanup_saves_the_text_but_leaves_the_message_alone(
    database: str, archive: list[Entry]
) -> None:
    """Чистка, пролежавшая под квотой сутки, не правит вчерашнее сообщение.

    Перерисовка собирает сообщение заново — вместе со строкой «⏰ Напомню
    в 19:40». Для напоминания, которое за эти сутки давно сработало, это
    обещание прошлого в будущем времени. Текст при этом сохраняется: он нужен
    поиску, сводке и вечернему разбору, где никакого «вчерашнего сообщения»
    нет вовсе.
    """
    notifier = FakeNotifier()
    entry_id = await _old_voice_with_transcript(
        entry(archive, WITH_REMINDER).text, age=timedelta(days=1)
    )

    await _clean(entry_id, _model(entry_id, "Вычищено спустя сутки."), notifier)

    stored = await repository.transcript_to_clean(entry_id)
    assert stored is not None
    assert stored.clean_text == "Вычищено спустя сутки."
    assert notifier.edited == [] and notifier.asked == [], "правится сообщение суточной давности"


@pytest.mark.integration
async def test_transcription_does_not_show_the_raw_version_while_cleanup_is_coming(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Мгновенно показанный мусор хуже небольшой задержки.

    Расшифровка ставит два задания: чистку — сразу, показ — на срок ожидания.
    Сама она не показывает ничего: иначе владелец увидел бы дословный вариант
    за секунды до вычищенного, и вся задача была бы напрасной.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)

    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)

    assert notifier.edited == [], "дословный вариант показан, не дождавшись чистки"
    assert len(await _jobs_of(CLEANUP)) == 1
    assert len(await _jobs_of(SHOW_TRANSCRIPT)) == 1


@pytest.mark.integration
async def test_owner_sees_the_verbatim_version_when_cleanup_is_late(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Не дождались — показываем дословный. Молчать над готовым текстом хуже."""
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    spoken = entry(archive, PLAIN).text
    await _transcribe(entry_id, spoken, notifier)

    await jobs.show_transcript(
        entry_id=entry_id, chat_id=CHAT_ID, ack_message_id=ACK_MESSAGE_ID, notifier=notifier
    )

    assert notifier.edited[-1][2] == spoken


@pytest.mark.integration
async def test_repeated_cleanup_redraws_without_paying_twice(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Повтор задания не платит за чистку второй раз, но сообщение перерисовывает.

    Молчаливый выход здесь оставил бы владельца с «Принял» навсегда: первый
    проход мог упасть между записью в базу и правкой сообщения, а задание
    показа по сроку, увидев готовый `clean_text`, тоже промолчит.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)
    await _clean(entry_id, _model(entry_id, "Вычищено."), notifier)

    second = _model(entry_id, "второй раз платить не за что")
    await _clean(entry_id, second, notifier)

    assert second.calls == 0
    assert notifier.edited[-1][2] == "Вычищено."


@pytest.mark.integration
async def test_late_show_stays_quiet_when_cleanup_already_spoke(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Чистка успела — срок ожидания не должен перерисовывать сообщение второй раз."""
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)
    await _clean(entry_id, _model(entry_id, "Вычищено."), notifier)
    shown_before = len(notifier.edited)

    await jobs.show_transcript(
        entry_id=entry_id, chat_id=CHAT_ID, ack_message_id=ACK_MESSAGE_ID, notifier=notifier
    )

    assert len(notifier.edited) == shown_before


@pytest.mark.integration
async def test_status_counts_the_backlog_without_counting_private_entries(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Отставание чистки — это то, что она **должна** была вычистить.

    Приватная запись в него не входит: чистить её нельзя вовсе (инвариант 5),
    её `clean_text` пуст навсегда, и, попав в счётчик, она вечно показывала бы
    отставание, которого нет.
    """
    notifier = FakeNotifier()
    # Первая: вычищена. С неё начинается отсчёт — всё, что появилось позже,
    # обязано быть вычищенным (записи до неё чистить не будут никогда).
    cleaned = await _capture_voice(tmp_path, message_id=1)
    await _transcribe(cleaned, entry(archive, PLAIN).text, notifier)
    await _clean(cleaned, _model(cleaned, "Вычищено."), notifier)

    # Вторая: чистка до неё не дошла — вот это и есть отставание.
    waiting = await _capture_voice(tmp_path, message_id=2)
    await _transcribe(waiting, entry(archive, PLAIN).text, notifier)

    # Третья: приватная задним числом — чистить нельзя, и это не отставание.
    private = await _capture_voice(tmp_path, message_id=3)
    await _transcribe(private, entry(archive, PLAIN).text, notifier)
    await overrides.make_private(private)

    collected = await status.collect(TIMEZONE)

    assert collected.transcripts_without_cleanup == 1


# --- Придерживание косметики под квотой ---


@pytest.mark.integration
async def test_cosmetics_are_allowed_until_the_first_refusal(database: str) -> None:
    assert await present.cosmetic_allowed(datetime.now(UTC)) is True


@pytest.mark.integration
async def test_cosmetics_stop_after_the_first_429(database: str) -> None:
    """Придерживание включается с первого отказа, а не по настроенному числу.

    Лимит текстовой модели неизвестен и настройка пуста намеренно. Механизм,
    ждущий настроенного числа, при пустой настройке молчал бы ровно в тот день,
    ради которого написан, — и выглядел бы при этом работающим.

    **Отказ ставится от начала суток провайдера, а не от «час назад».**
    Так было до 01.09.2026, и это давало тест, падающий **один час в сутки**:
    с полуночи по Тихому океану до неё же плюс час «час назад» приходится
    на **вчерашние** сутки провайдера, а придерживание считается по текущим
    (ADR-0014). Поймано прогоном в 07:24 UTC — то есть внутри этого часа;
    в любое другое время суток тест был зелёным и врал.

    Тот же почерк, что у трёх тестов вечернего разбора 24.08.2026: время
    в тесте бралось из настоящих часов, а утверждение молчаливо предполагало
    определённое время суток.
    """
    now = datetime.now(UTC)
    await _refused(status=429, at=quota.window_start(now) + timedelta(minutes=1))

    assert await present.cosmetic_allowed(now) is False


@pytest.mark.integration
async def test_hold_lifts_when_the_pacific_day_rolls_over(database: str) -> None:
    """Сутки у провайдера тихоокеанские — счёт по полуночи владельца
    ошибался бы на девять часов (ADR-0014)."""
    now = datetime.now(UTC)
    await _refused(status=429, at=quota.window_start(now) - timedelta(minutes=1))

    assert await present.cosmetic_allowed(now) is True


@pytest.mark.integration
async def test_a_refusal_that_is_not_quota_does_not_hold_cosmetics(database: str) -> None:
    """`500` проходит сам за секунды — придерживать из-за него косметику незачем."""
    now = datetime.now(UTC)
    await _refused(status=500, at=now - timedelta(minutes=1))

    assert await present.cosmetic_allowed(now) is True


@pytest.mark.integration
async def test_cleanup_waits_out_the_quota_instead_of_dying(
    database: str, archive: list[Entry], tmp_path: Path
) -> None:
    """Косметическое задание переставляет себя, а не исчерпывает попытки.

    Пять попыток с ростом задержки — это около получаса, а квота сбрасывается
    через часы: без переноса задание ушло бы в `failed` и ждало бы рук.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(tmp_path)
    await _transcribe(entry_id, entry(archive, PLAIN).text, notifier)
    now = datetime.now(UTC)
    await _refused(status=429, at=now)

    model = _model(entry_id, "не должно уйти")
    await _clean(entry_id, model, notifier)

    assert model.calls == 0
    postponed = await _jobs_of(CLEANUP)
    assert len(postponed) == 2, "задание не переставлено"
    assert abs(postponed[-1][1] - quota.resets_at(now)) < timedelta(minutes=1)
