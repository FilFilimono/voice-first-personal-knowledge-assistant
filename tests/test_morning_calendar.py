"""Календарь первой строкой утренней сводки — от тика до текста владельцу.

**Почему путь, а не рендер.** Чистая функция `render.morning` была бы зелёной
и в том случае, если бы `morning.collect` никогда не спрашивала календарь:
дыра ровно этой формы уже стоила проекту месяца (`loops.is_stuck` — написана,
покрыта тестом, не звалась из `src/` ни разу). Поэтому каждая проверка здесь
начинается с `service.tick` и кончается тем, что получил `FakeNotifier`.

Четыре утверждения, и каждое ломается по-своему:

1. встречи стоят **первой строкой** — до материала, до тем и до дел;
2. **отказ календаря сводку не роняет** — она уходит без первой строки,
   а не с заглушкой «календарь недоступен»;
3. **встреч нет — строки нет вовсе**: ни «сегодня встреч нет», ни пустого
   значка (`vision.md` §7.0, принцип 3);
4. **календарь поводом отправить сводку не является** — день без дел и без
   петли молчит, сколько бы встреч в нём ни стояло.

**Про даты.** Момент передаётся в `service.tick` параметром, и весь путь —
сводка, календарь, дела — считает от него. Настоящие часы читает единственное
место (`touches.record_sent`, поле `sent_at`), и ни одно утверждение здесь
на него не смотрит. Поэтому прибитая дата тут безопасна, и это проверено
чтением кода, а не предположением (`CLAUDE.md`, правило часов).

Ни одного живого вызова: календарь — заглушка, эмбеддер — заглушка, модель
сводки не зовётся вовсе (`model=None`).

Требует поднятого Postgres: `just up`.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from brain.core import ingest
from brain.core.digest import event_context, service
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import CalendarEvent, CalendarRef
from brain.storage.models.annotation import AnnotationState, AnnotationType
from tests.fakes import FakeNotifier, StubEmbedder

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 4242

# 09:30 по Риму — внутри утреннего окна, тот же момент, что в соседних тестах.
MORNING = datetime(2026, 9, 7, 7, 30, tzinfo=UTC)

# Встречи дня. Обе **вне** окна контекста перед событием (15–30 минут),
# чтобы этот файл проверял только первую строку сводки: 13:00 и 17:30 по Риму.
STANDUP = "Планёрка по резюме"
CALL = "Созвон с подрядчиком"

DEED = "купить хлеб и позвонить маме"


class FakeCalendar:
    """Календарь, который никуда не ходит. Умеет и ответить, и упасть."""

    def __init__(self, events: Sequence[CalendarEvent] = (), *, broken: bool = False) -> None:
        self._events = list(events)
        self._broken = broken
        self.windows: list[tuple[datetime, datetime]] = []

    async def calendars(self) -> Sequence[CalendarRef]:  # pragma: no cover — сводке не нужны
        return []

    async def events(self, *, since: datetime, until: datetime) -> Sequence[CalendarEvent]:
        self.windows.append((since, until))
        if self._broken:
            raise RuntimeError("календарь не ответил")
        return [item for item in self._events if since <= item.starts_at < until]

    async def create_calendar(self, *, title: str, timezone: str) -> CalendarRef:
        raise NotImplementedError  # pragma: no cover — чтение сюда не заходит

    async def create_event(
        self,
        *,
        calendar_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str,
    ) -> CalendarEvent:
        raise NotImplementedError  # pragma: no cover — чтение сюда не заходит


def meeting(title: str, *, at: datetime, all_day: bool = False) -> CalendarEvent:
    """Событие календаря в том виде, в каком его отдаёт порт: всегда UTC."""
    return CalendarEvent(
        id=f"event-{title}",
        calendar_id="primary",
        calendar_title="Основной",
        title=title,
        starts_at=at,
        ends_at=at + timedelta(hours=1),
        all_day=all_day,
        location=None,
        attendees=(),
    )


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Эмбеддер — заглушка: контекст перед событием ходит в поиск.

    В этом файле он до отправки не доходит (встречи вне окна), но подменяется
    всё равно: тест, который **может** сходить в сеть, однажды сходит.
    """
    monkeypatch.setattr(event_context, "embedder", StubEmbedder)


def with_calendar(monkeypatch: pytest.MonkeyPatch, calendar: FakeCalendar) -> FakeCalendar:
    """Подставить календарь на тот же шов, которым его собирает воркер."""
    monkeypatch.setattr(event_context, "client", lambda: calendar)
    return calendar


async def _a_deed() -> None:
    """Дело так, как его заводит разметка: из единицы записи.

    Нужно затем, чтобы сводке было о чём говорить: календарь поводом отправить
    её не является, и без дела проверять первую строку было бы не в чем.
    """
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(chat_id=CHAT_ID, message_id=10, ack_message_id=11, text=DEED),
            is_private=False,
            timezone=TIMEZONE,
            now=MORNING - timedelta(days=1),
        )
    assert result.entry_id is not None

    await enrichment.save_annotation(
        entry_id=result.entry_id,
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
                end=len(DEED),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=DEED,
    )


async def _summary(monkeypatch: pytest.MonkeyPatch, calendar: FakeCalendar) -> str | None:
    """Прогнать тик и вернуть текст сводки. `None` — сводка не пришла."""
    with_calendar(monkeypatch, calendar)
    notifier = FakeNotifier()

    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    if not notifier.mornings:
        return None
    return notifier.mornings[0][1]


async def test_todays_meetings_stand_first_in_the_morning_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Первая строка — расписание, и стоит она до всего остального.

    Порядок проверяется по месту в тексте, а не по факту наличия: строка,
    приехавшая под делами, тоже «есть», и утверждение «календарь показывается»
    было бы выполнено — при том что решение владельца от 07.09.2026 именно
    про место.
    """
    await _a_deed()
    text = await _summary(
        monkeypatch,
        FakeCalendar(
            [
                meeting(STANDUP, at=MORNING + timedelta(hours=4)),
                meeting(CALL, at=MORNING + timedelta(hours=6, minutes=30)),
            ]
        ),
    )

    assert text is not None
    lines = [line for line in text.splitlines() if line.strip()]

    # Первая строка после шапки — и она про сегодняшние встречи.
    assert lines[0] == "☀️ Утро."
    assert lines[1].startswith("🗓 Сегодня:")

    # Время — в поясе владельца, а не в UTC: 11:30 и 16:00 были бы «правдой»
    # в другом часовом поясе, и это самая дорогая ошибка этого слоя.
    assert "13:30 Планёрка по резюме" in lines[1]
    assert "16:00 Созвон с подрядчиком" in lines[1]

    # И она стоит выше дел — того, что владелец должен.
    assert text.index("🗓") < text.index("Висит:")


async def test_a_calendar_that_does_not_answer_costs_the_line_and_not_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Календарь лёг — сводка уходит без первой строки, как уходила до фазы.

    Заглушки «календарь недоступен» здесь нет намеренно: это шум в единственном
    сообщении, которое приходит каждый день, и починить по нему владелец всё
    равно ничего не сможет — состояние календаря видно в `/status`.
    """
    await _a_deed()
    text = await _summary(monkeypatch, FakeCalendar(broken=True))

    assert text is not None
    assert "Висит:" in text
    assert "🗓" not in text
    assert "календар" not in text.lower()


async def test_a_day_without_meetings_says_nothing_about_meetings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Встреч нет — строки нет вовсе (`vision.md` §7.0, принцип 3)."""
    await _a_deed()
    text = await _summary(monkeypatch, FakeCalendar([]))

    assert text is not None
    assert "Висит:" in text
    assert "🗓" not in text
    assert "встреч" not in text.lower()


async def test_the_calendar_alone_is_not_a_reason_to_send_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ни дел, ни петли — сводки нет, сколько бы встреч ни стояло в дне.

    Тот же довод, что у среза по темам: сводка, состоящая из одного расписания,
    — это пересказ того, что владелец и так видит в календаре, и приходила бы
    она каждый рабочий день.
    """
    text = await _summary(
        monkeypatch, FakeCalendar([meeting(STANDUP, at=MORNING + timedelta(hours=4))])
    )

    assert text is None


async def test_an_all_day_event_is_shown_without_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """У события на весь день времени нет — и 00:00 у него показывать нельзя.

    Полночь в такой строке читается как настоящий час встречи, то есть даёт
    правдоподобный и неверный результат — ровно та ошибка, ради которой
    `CalendarEvent.all_day` вообще существует.
    """
    await _a_deed()
    day = MORNING.astimezone(UTC).replace(hour=22, minute=0) - timedelta(days=1)
    text = await _summary(monkeypatch, FakeCalendar([meeting("Отпуск", at=day, all_day=True)]))

    assert text is not None
    assert "🗓 Сегодня: Отпуск" in text
    assert "00:00" not in text
