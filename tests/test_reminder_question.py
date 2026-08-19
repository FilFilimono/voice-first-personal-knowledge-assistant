"""Запись похожа на напоминание, но слова «напомни» в ней нет.

Так появилась запись 27: расшифровка съела первую букву у «Напомни»,
и осталось «Помни …». Триггер за такое не цепляется — и правильно,
«помни» это законное самостоятельное слово. Плохо было другое: запись легла
в архив **молча**, и владелец узнал об этом, не дождавшись напоминания.

Признак — повелительный глагол из явного списка. Морфологию русского без
библиотеки надёжно не разобрать, а библиотека — новая зависимость и ADR;
список же объясним словами и пополняется одной строкой. Цена названа вслух:
незнакомый глагол пропускается молча.

Дальше развилка, и обе ветки обязаны говорить:

- время разобралось → «поставить на 18.08 в 19:40?» с кнопками да/нет;
- время не разобралось → «когда напомнить?», ответ приходит текстом.

Вторая ветка существует потому, что «не забудь позвонить маме» — очевидная
просьба без единого признака времени. Промолчать здесь значит воспроизвести
ровно ту дыру, которую чиним.
"""

from datetime import UTC, datetime

import pytest

from brain.core.scheduler import reminders
from brain.core.scheduler.queue import REMINDER
from tests.archive import Entry, entry
from tests.fakes import FakeUnitOfWork

TIMEZONE = "Europe/Moscow"
CHAT_ID = 111
ENTRY_ID = 7

# Вторник, 18 августа 2026, 13:08 по Москве.
NOW = datetime(2026, 8, 18, 10, 8, tzinfo=UTC)


async def _schedule(text: str, *, now: datetime = NOW) -> reminders.ScheduleOutcome | None:
    uow = FakeUnitOfWork()
    return await reminders.schedule(
        uow, entry_id=ENTRY_ID, chat_id=CHAT_ID, text=text, timezone=TIMEZONE, now=now
    )


# --- Ветка 1: время известно, спрашиваем да/нет ---

# entries.id = 27, голос. Ровно та запись, из-за которой всё это заведено.
# Строка придумана по форме записи 27, а не скопирована из неё: сам текст
# живёт в базе и в репозиторий не попадает (ADR-0002). Здесь важна форма —
# повелительный глагол, времени суток нет, слова «напомни» нет, — а не то,
# что именно владелец сказал в тот вечер. Живая речь проверяется отдельно,
# в `test_reminders_live_speech.py`, и там текст приезжает из базы.
LOST_TO_STT = "Помни выйти в 19:40."


async def test_imperative_with_a_time_asks_for_confirmation() -> None:
    outcome = await _schedule(LOST_TO_STT)

    assert outcome is not None, "запись снова легла в архив молча"
    assert outcome.ask is reminders.Ask.CONFIRM
    assert not outcome.created
    # Предложенное время видно сразу — иначе «да» жмут вслепую.
    assert "19:40" in outcome.message


async def test_confirmation_question_does_not_create_anything_yet() -> None:
    """Вопрос — это вопрос. Напоминание появляется только после «да»."""
    uow = FakeUnitOfWork()

    await reminders.schedule(
        uow, entry_id=ENTRY_ID, chat_id=CHAT_ID, text=LOST_TO_STT, timezone=TIMEZONE, now=NOW
    )

    assert uow.reminders == []
    assert uow.jobs == []


# --- Ветка 2: времени нет, спрашиваем «когда» ---


@pytest.mark.parametrize(
    "text",
    [
        "не забудь позвонить маме",
        "купи молока",
        "проверь почту",
    ],
)
async def test_imperative_without_a_time_asks_when(text: str) -> None:
    """Глагол есть, времени нет — смысл очевиден, промолчать нельзя."""
    outcome = await _schedule(text)

    assert outcome is not None, "запись легла в архив молча"
    assert outcome.ask is reminders.Ask.WHEN
    assert not outcome.created
    assert "огда напомнить" in outcome.message


async def test_when_question_repeats_what_it_is_about() -> None:
    """Вопрос без сути — «когда напомнить?» про что? — бесполезен."""
    outcome = await _schedule("не забудь позвонить маме")

    assert outcome is not None
    assert "позвонить маме" in outcome.message


# --- Подтверждение: «да» и текстовый ответ приводят к напоминанию ---


async def test_confirmed_reminder_is_created_from_the_entry_itself() -> None:
    """«Да» — время берётся из самой записи."""
    uow = FakeUnitOfWork()

    outcome = await reminders.schedule_confirmed(
        uow,
        entry_id=ENTRY_ID,
        chat_id=CHAT_ID,
        subject=LOST_TO_STT,
        when=LOST_TO_STT,
        timezone=TIMEZONE,
        now=NOW,
    )

    assert outcome.created
    draft = uow.reminders[0]
    assert draft.source_entry_id == ENTRY_ID
    assert (draft.fire_at.astimezone(draft.fire_at.tzinfo).hour, draft.fire_at.minute) == (19, 40)
    # Напоминание и задание — одной транзакцией (инвариант 7).
    assert uow.jobs_named(REMINDER)


async def test_answer_supplies_the_time_and_the_entry_supplies_the_subject() -> None:
    """Ответ «сегодня в 19:00» относится к тому, о чём спрашивали."""
    uow = FakeUnitOfWork()

    outcome = await reminders.schedule_confirmed(
        uow,
        entry_id=ENTRY_ID,
        chat_id=CHAT_ID,
        subject="не забудь позвонить маме",
        when="сегодня в 19:00",
        timezone=TIMEZONE,
        now=NOW,
    )

    assert outcome.created
    draft = uow.reminders[0]
    assert draft.fire_at.astimezone(draft.fire_at.tzinfo).hour == 19
    assert "позвонить маме" in draft.text
    assert "забудь" not in draft.text


async def test_unparsable_answer_says_so_and_creates_nothing() -> None:
    """Ответ не разобран — говорим вслух, а не заводим наугад."""
    uow = FakeUnitOfWork()

    outcome = await reminders.schedule_confirmed(
        uow,
        entry_id=ENTRY_ID,
        chat_id=CHAT_ID,
        subject="не забудь позвонить маме",
        when="как-нибудь потом",
        timezone=TIMEZONE,
        now=NOW,
    )

    assert not outcome.created
    assert uow.reminders == []
    assert uow.jobs == []
    assert "не понял" in outcome.message.lower()


# --- Ложных вопросов быть не должно ---

# Записи архива без слова «напомни». Все содержат время или день, и ни одна
# не является просьбой: это планы, отчёты и рассуждения вслух.
#
# Только номера: текст живёт в базе и в репозиторий не попадает (ADR-0002).
# Что в каждой — в идентификаторе параметра, чтобы падение читалось без базы.
ORDINARY_THOUGHTS = [
    pytest.param(8, id="экзамен во вторник"),
    pytest.param(15, id="перенёс CV чек"),
    pytest.param(19, id="одно время без глагола"),
    pytest.param(7, id="планы на сегодня"),
    pytest.param(13, id="надо будет разобрать"),
    pytest.param(25, id="в офисе до вечера"),
    pytest.param(22, id="нужно всё вырубить"),
]


@pytest.mark.parametrize("entry_id", ORDINARY_THOUGHTS)
async def test_ordinary_thought_is_not_questioned(archive: list[Entry], entry_id: int) -> None:
    """Инфинитив и «надо» — не повелительное наклонение.

    Семь записей архива содержат время без просьбы. Спрашивать про каждую
    значит превратить приём мысли в допрос — и обесценить вопрос там,
    где он нужен.
    """
    assert await _schedule(entry(archive, entry_id).text) is None


async def test_explicit_reminder_still_goes_the_usual_way() -> None:
    """Со словом «напомни» вопросов не задаём — там всё однозначно."""
    outcome = await _schedule("напомни в 19:40 уйти домой")

    assert outcome is not None
    assert outcome.created
    assert outcome.ask is reminders.Ask.NOTHING
