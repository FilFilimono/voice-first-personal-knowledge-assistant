"""Инвариант 5 на пути в календарь: приватное наружу не уходит никогда.

**Проверка живёт в `integrations`, а не у вызывающего** — то же место и то же
правило, что у `core/llm` и `core/stt`. Здесь она проверяется по-настоящему:
живая база, настоящий `events.create_for_entry`, а заглушка стоит только
на месте Google — и она **считает обращения**. Утверждение «наружу не ушло»
доказывается нулём вызовов, а не текстом ответа.

Два конца, и один другого не заменяет:

    кнопки нет вовсе          `events.offered` — про удобство. Кнопка, которая
                              отвечает отказом, хуже отсутствующей;
    проверка при создании     инвариант 5. Работает даже тогда, когда запись
                              сделали приватной **после** показа кнопки.

Второе без первого — раздражение, первое без второго — дыра. Поэтому здесь
есть тест ровно на тот случай, который ловит только второе: кнопка получена,
потом нажата «🔒 Приватная», потом нажата «в календарь». Приватность
в этом проекте ретроактивна (ADR-0002), и путь этот не выдуманный.

**Про часы.** Момент берётся от настоящих — `ingest.capture_text` разбирает
«напомни завтра в 19:00» относительно него, и прибитая дата однажды уехала бы
за границу суток. Правило проекта: продакшн смотрит на настоящие часы — тест
обязан смотреть на них же.

Требует поднятого Postgres: `just up`.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from brain.config import get_settings
from brain.core import ingest
from brain.core.memory import overrides
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import CalendarEvent, CalendarRef
from brain.integrations.gcal import events
from brain.storage.db import session_scope
from brain.storage.models.entry_calendar_event import EntryCalendarEvent

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 4242
TARGET = "brain@group.calendar.google.com"

WITH_TIME = "напомни завтра в 19:00 позвонить маме"
WITHOUT_TIME = "подумал, что архитектура у нас так себе"


class CountingCalendar:
    """Google, который считает, сколько раз к нему сходили.

    Счётчик здесь важнее ответа: главное утверждение файла — «приватная запись
    наружу не уходит» — доказывается нулём обращений, а не разбором того,
    что мы отправили.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    async def calendars(self) -> Sequence[CalendarRef]:  # pragma: no cover — не нужен
        return []

    async def events(  # pragma: no cover — запись сюда не заходит
        self, *, since: datetime, until: datetime
    ) -> Sequence[CalendarEvent]:
        return []

    async def create_calendar(self, *, title: str, timezone: str) -> CalendarRef:
        raise NotImplementedError  # pragma: no cover — календарь уже заведён

    async def create_event(
        self,
        *,
        calendar_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str,
    ) -> CalendarEvent:
        self.created.append((calendar_id, title))
        return CalendarEvent(
            id=f"gcal-{len(self.created)}",
            calendar_id=calendar_id,
            calendar_title="Brain",
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=False,
            location=None,
            attendees=(),
        )


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Календарь настроен: четыре переменные, как их кладёт владелец в `.env`."""
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "брайн.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "секрет")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "1//0gрефреш")
    monkeypatch.setenv("GOOGLE_CALENDAR_TARGET_ID", TARGET)
    get_settings.cache_clear()


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> CountingCalendar:
    calendar = CountingCalendar()
    monkeypatch.setattr(events, "build_calendar", lambda settings: calendar)
    return calendar


async def capture(text: str, *, private: bool, message_id: int = 10) -> int:
    """Принять запись так, как её принимает бот. Возвращает её номер."""
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(
                chat_id=CHAT_ID,
                message_id=message_id,
                ack_message_id=message_id + 1,
                text=text,
            ),
            is_private=private,
            timezone=TIMEZONE,
            now=datetime.now(UTC),
        )
    assert result.entry_id is not None
    return result.entry_id


async def provenance(entry_id: int) -> list[EntryCalendarEvent]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(EntryCalendarEvent).where(EntryCalendarEvent.source_entry_id == entry_id)
        )
        return list(rows.all())


# --- Обычная запись: событие создаётся и оставляет след ---


async def test_a_normal_entry_goes_into_the_brain_calendar_with_provenance(
    google: CountingCalendar,
) -> None:
    """Событие уходит в целевой календарь и оставляет строку с `source_entry_id`.

    Провенанс — инвариант 4, и он же доказательство критерия фазы: строка
    здесь появляется только на пути от нажатия.
    """
    entry_id = await capture(WITH_TIME, private=False)

    assert await events.offered(entry_id) is True

    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.CREATED
    assert result.calendar == "Brain"
    # Ушло **в тот календарь, который задан настройкой**, а не в личный.
    assert [where for where, _ in google.created] == [TARGET]

    (row,) = await provenance(entry_id)
    assert row.source_entry_id == entry_id
    assert row.calendar_id == TARGET
    assert row.google_event_id == "gcal-1"
    # Длительность — та, что выбрана в `events.DURATION`.
    assert row.ends_at - row.starts_at == events.DURATION


async def test_a_second_press_does_not_create_a_second_event(google: CountingCalendar) -> None:
    """Кнопка живёт в переписке вечно, нажать её можно сколько угодно раз."""
    entry_id = await capture(WITH_TIME, private=False)
    await events.create_for_entry(entry_id, now=datetime.now(UTC))

    again = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert again.outcome is events.Outcome.ALREADY
    assert len(google.created) == 1
    assert len(await provenance(entry_id)) == 1
    # И кнопку больше не предлагаем.
    assert await events.offered(entry_id) is False


# --- Приватность ---


async def test_a_private_entry_never_reaches_google(google: CountingCalendar) -> None:
    """Инвариант 5. Доказывается нулём обращений, а не текстом отказа."""
    entry_id = await capture(WITH_TIME, private=True)

    assert await events.offered(entry_id) is False, "кнопки у приватной записи нет вовсе"

    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.PRIVATE
    assert google.created == [], "в Google не сходили ни разу"
    assert await provenance(entry_id) == []


async def test_privacy_switched_on_after_the_button_still_stops_the_event(
    google: CountingCalendar,
) -> None:
    """Тот самый путь, который ловит только проверка в `integrations`.

    Кнопка получена законно — запись была обычной. Потом владелец нажал
    «🔒 Приватная». Потом нажал «в календарь». Отсутствие кнопки здесь
    не спасает: кнопка уже показана и живёт в переписке вечно.

    Приватность в этом проекте ретроактивна (ADR-0002) именно потому, что
    человек не всегда понимает в момент записи, что записал.
    """
    entry_id = await capture(WITH_TIME, private=False)
    assert await events.offered(entry_id) is True

    await overrides.make_private(entry_id)

    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.PRIVATE
    assert google.created == []
    assert await provenance(entry_id) == []


# --- Нечего класть ---


async def test_an_entry_without_a_reminder_is_not_offered(google: CountingCalendar) -> None:
    """Обычная мысль в календарь не просится: класть туда нечего."""
    entry_id = await capture(WITHOUT_TIME, private=False, message_id=20)

    assert await events.offered(entry_id) is False

    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.NO_REMINDER
    assert google.created == []


async def test_an_unconfigured_calendar_refuses_before_touching_the_network(
    google: CountingCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не настроен — состояние, а не ошибка, и до сети дело не доходит."""
    monkeypatch.delenv("GOOGLE_CALENDAR_REFRESH_TOKEN", raising=False)
    get_settings.cache_clear()

    entry_id = await capture(WITH_TIME, private=False)

    assert await events.offered(entry_id) is False

    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.OFF
    assert google.created == []


# --- Отказ Google ---


async def test_a_refusal_leaves_the_entry_and_the_reminder_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Календарь лёг — запись цела, напоминание внутри бота работает.

    То же правило, по которому косметическая модель не имеет права валить старт
    воркера: без календаря система работает, без захвата — нет. Наверх отказ
    не поднимается, и строки провенанса не оставляет.
    """
    from brain.integrations.gcal import errors

    class Refusing(CountingCalendar):
        async def create_event(
            self,
            *,
            calendar_id: str,
            title: str,
            starts_at: datetime,
            ends_at: datetime,
            description: str,
        ) -> CalendarEvent:
            raise errors.InvalidGrantError("токен отозван", status=400)

    monkeypatch.setattr(events, "build_calendar", lambda settings: Refusing())

    entry_id = await capture(WITH_TIME, private=False)
    result = await events.create_for_entry(entry_id, now=datetime.now(UTC))

    assert result.outcome is events.Outcome.FAILED
    assert "Testing" in result.detail, "совет обязан назвать самую частую причину"
    assert await provenance(entry_id) == []
    # Кнопка остаётся предложенной: отказ временный по своей природе, и после
    # повторной авторизации то же нажатие обязано сработать.
    assert await events.offered(entry_id) is True


async def test_the_event_lasts_the_chosen_duration() -> None:
    """Длительность названа числом и выбрана, а не измерена — см. `events.DURATION`."""
    assert timedelta(minutes=30) == events.DURATION
