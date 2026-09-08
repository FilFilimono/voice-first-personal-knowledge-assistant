"""Контекст перед событием — от тика до сообщения владельцу (`vision.md` §5.7).

**Почему путь, а не `collect`.** Три слоя можно собрать чистой функцией
и покрыть её тестом, и он будет зелёным даже если `deliver_due` не зовётся
из тика ни разу. Дыра ровно этой формы стоила проекту месяца: `loops.is_stuck`
была написана, покрыта тестом и не вызывалась из `src/`. Поэтому каждая
проверка здесь начинается с `service.tick`.

Что утверждается:

1. контекст приходит внутри окна 15–30 минут — и **со звуком**: это заказанное
   касание (`vision.md` §7.1), а `send_quietly` здесь был бы ошибкой,
   которую не видно ни в одном тесте на текст;
2. **и приходит один раз.** Тик идёт каждые пять минут, окно длится
   пятнадцать: без отсечки владелец получил бы одно и то же трижды;
3. вне окна — молчание: и до него, и после начала встречи;
4. **упавшая отправка повторяется следующим тиком.** Строка с пустым
   `delivered_at` — честное «не дошло», и это прямое следствие находки
   7 сентября 2026, когда `touches.sent_at` писался до отправки и сутки
   сгорали молча с подтверждением в базе;
5. нечего показать ни в одном слое — молчим (§7.0, принцип 3);
6. **пауза контекст не глушит.** «Замолчи на неделю» снимает инициативные
   касания и утреннюю сводку, но не то, о чём владелец просил сам (§7.1);
7. **ветка модели работает с пути тика** — с флагом, включённым в тесте.
   Код за выключенным флагом, который никто не зовёт, — это `loops.is_stuck`;
8. **потолок вызовов за сутки задаётся кодом**: исчерпан — уходит сухой
   рендер, а контекст всё равно приходит.

**Про часы, и это здесь не формальность.** Момент передаётся в `service.tick`
параметром, но `calls_today` сравнивает `LlmCall.created_at` — колонку, которую
заполняет **сервер базы** своими часами — с окном суток, посчитанным
от переданного `now`. Две разные пары часов в одном сравнении: прибей дату
в будущем, и потолок перестанет срабатывать, а тест останется зелёным
и будет утверждать неправду. Поэтому момент здесь берётся от настоящих часов,
как того требует правило проекта, а встречи расставляются относительно него.

Ни одного живого вызова: календарь — заглушка, эмбеддер — заглушка, модель —
сценарий.

Требует поднятого Postgres: `just up`.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from brain.config import get_settings
from brain.core import ingest
from brain.core.digest import event_context, quiet, service
from brain.core.enrich import repository as enrichment
from brain.core.enrich.units import MergedUnit
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import CalendarEvent, CalendarRef
from brain.storage.models.annotation import AnnotationState, AnnotationType
from tests.fakes import FakeNotifier, ScriptedModel, StubEmbedder

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 4242

MEETING = "Планёрка по резюме"
# Дело, которое совпадает с встречей по началу слова: «резюме» → «резю».
# Совпадение по началам, а не по словам целиком, — решение `_stems`.
DEED = "дописать резюме и отправить его в понедельник"
# Встреча, у которой в архиве нет ничего: третий слой пуст, и все остальные
# тоже — значит, молчим.
NOTHING = "Стрижка"

# Фраза без упрёка намеренно. Первая версия этого теста несла «резюме ты так
# и не дописал», и её отверг гвард — правильно: «так и не» стоит в стоп-списке
# `present/guard`, потому что это ровно ретроспективный упрёк, запрещённый
# `vision.md` §7.0, принцип 2. Тест был неправ, код прав; оставлено здесь,
# чтобы следующий автор не «чинил» гвард под свою фразу.
PHRASE = "на планёрке пригодится то, что ты записывал про резюме"


def now() -> datetime:
    """Настоящий момент. Прибитой даты в этом файле нет — см. шапку модуля."""
    return datetime.now(UTC)


class FakeCalendar:
    """Календарь, который никуда не ходит."""

    def __init__(self, events: Sequence[CalendarEvent] = ()) -> None:
        self._events = list(events)

    async def calendars(self) -> Sequence[CalendarRef]:  # pragma: no cover — не нужен
        return []

    async def events(self, *, since: datetime, until: datetime) -> Sequence[CalendarEvent]:
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


class BrokenNotifier(FakeNotifier):
    """Канал, у которого падает именно отправка контекста.

    Нужен затем, чтобы проверить повтор: `send` — тот же метод, которым уходит
    заказанное касание, и его отказ обязан оставить строку без `delivered_at`,
    а не отменить встречу.
    """

    async def send(self, chat_id: int, text: str) -> int:
        raise RuntimeError("телеграм не принял")


def meeting(title: str, *, at: datetime, event_id: str | None = None) -> CalendarEvent:
    return CalendarEvent(
        id=event_id or f"event-{title}",
        calendar_id="primary",
        calendar_title="Основной",
        title=title,
        starts_at=at,
        ends_at=at + timedelta(hours=1),
        all_day=False,
        location=None,
        attendees=(),
    )


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_context, "embedder", StubEmbedder)


def with_calendar(monkeypatch: pytest.MonkeyPatch, *events: CalendarEvent) -> None:
    """Подставить календарь тем же швом, которым его собирает воркер."""
    calendar = FakeCalendar(events)
    monkeypatch.setattr(event_context, "client", lambda: calendar)


async def a_deed(moment: datetime, text: str = DEED) -> None:
    """Незакрытое дело по теме встречи — третий слой `vision.md` §5.7."""
    async with capture_transaction() as uow:
        result = await ingest.capture_text(
            uow,
            ingest.IncomingText(chat_id=CHAT_ID, message_id=10, ack_message_id=11, text=text),
            is_private=False,
            timezone=TIMEZONE,
            now=moment - timedelta(days=1),
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
                end=len(text),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=text,
    )


async def run_tick(
    moment: datetime,
    *,
    notifier: FakeNotifier | None = None,
    model: ScriptedModel | None = None,
) -> FakeNotifier:
    """Один тик. Возвращает канал, чтобы смотреть, что именно ушло."""
    channel = notifier or FakeNotifier()
    await service.tick(
        now=moment, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=channel, model=model
    )
    return channel


def contexts(channel: FakeNotifier) -> list[str]:
    """Сообщения контекста: они уходят `send`, а не `send_quietly`."""
    return [text for _, text in channel.sent if text.startswith("🗓")]


# --- Окно ---


async def test_the_context_arrives_inside_the_window_and_with_sound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Двадцать минут до встречи — пора говорить, и говорить вслух."""
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=20)))

    channel = await run_tick(moment)

    (text,) = contexts(channel)
    assert MEETING in text
    assert "резюме" in text
    # Со звуком: заказанное касание (`vision.md` §7.1). Беззвучное исключение
    # есть только у привычек.
    assert not [one for one in channel.quiet if one[1].startswith("🗓")]


async def test_a_meeting_beyond_the_window_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """За сорок минут говорить рано: контекст обязан быть свежим."""
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=40)))

    assert contexts(await run_tick(moment)) == []


async def test_a_meeting_already_started_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Начавшаяся встреча закрывает окно: контекст к ней уже не контекст."""
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment - timedelta(minutes=5)))

    assert contexts(await run_tick(moment)) == []


# --- Один раз, а не трижды ---


async def test_the_same_meeting_is_not_told_about_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тик идёт каждые пять минут, окно длится пятнадцать.

    Без отсечки владелец получил бы одно и то же сообщение три раза подряд.
    Ровно ради этого заведена таблица `event_contexts`, и проверяется здесь
    не её содержимое, а то, что она **используется**.
    """
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=25)))

    first = await run_tick(moment)
    second = await run_tick(moment + timedelta(minutes=5))
    third = await run_tick(moment + timedelta(minutes=10))

    assert len(contexts(first)) == 1
    assert contexts(second) == []
    assert contexts(third) == []


async def test_a_failed_send_is_retried_by_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Упавшая отправка не сжигает встречу — в отличие от `touches.sent_at`.

    Находка 7 сентября 2026: отметка, поставленная до отправки, делает упавшую
    отправку неотличимой от состоявшейся. Здесь отметок две, и повтор идёт
    именно по пустой `delivered_at`.
    """
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=25)))

    # Первый тик падает на отправке — тик огораживает отказ и идёт дальше.
    await run_tick(moment, notifier=BrokenNotifier())
    assert await event_context.delivered_ids(["event-Планёрка по резюме"]) == set()

    second = await run_tick(moment + timedelta(minutes=5))

    assert len(contexts(second)) == 1
    assert await event_context.delivered_ids(["event-Планёрка по резюме"]) == {
        "event-Планёрка по резюме"
    }


# --- Молчание ---


async def test_nothing_to_show_means_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Через двадцать минут встреча» содержанием не является.

    Владелец видит это в своём календаре, и сообщение об этом было бы
    сообщением ни о чём (`vision.md` §7.0, принцип 3).
    """
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(NOTHING, at=moment + timedelta(minutes=20)))

    assert contexts(await run_tick(moment)) == []


async def test_the_pause_does_not_silence_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Замолчи на неделю» глушит инициативные касания, а не заказанные.

    Система не имеет права молчать о том, о чём её просили сами (`vision.md`
    §7.1). Проверяется тем же тиком: пауза стоит, сводка не уходит, контекст
    уходит.
    """
    moment = now()
    await a_deed(moment)
    await quiet.pause(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=20)))

    channel = await run_tick(moment)

    assert len(contexts(channel)) == 1
    assert channel.mornings == []


# --- Кто пишет текст ---


async def test_the_model_is_not_called_while_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Умолчание — сухой рендер: контекст не стоит запроса из двадцати суточных."""
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=20)))
    writer = ScriptedModel([{"phrase": PHRASE}])

    channel = await run_tick(moment, model=writer)

    (text,) = contexts(channel)
    assert writer.calls == 0
    assert PHRASE not in text
    # Сухой рендер при этом полноценный: слой на месте.
    assert "резюме" in text


async def test_the_model_writes_the_phrase_from_the_tick_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ветка за флагом обязана работать с пути тика, иначе это `loops.is_stuck`."""
    moment = now()
    await a_deed(moment)
    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=20)))
    monkeypatch.setenv("EVENT_CONTEXT_WRITTEN", "true")
    get_settings.cache_clear()
    writer = ScriptedModel([{"phrase": PHRASE}])

    channel = await run_tick(moment, model=writer)

    (text,) = contexts(channel)
    assert writer.calls == 1
    assert PHRASE in text
    # Строки материала остаются кодом при любом раскладе: модель пишет только
    # фразу (ADR-0015).
    assert "резюме" in text


async def test_the_daily_cap_falls_back_to_the_dry_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Потолок задаётся кодом, а не намерением.

    Второй встрече фраза уже не достаётся, но контекст к ней **приходит**:
    потолок стоит вызовов модели, а не заказанного касания.
    """
    moment = now()
    await a_deed(moment)
    monkeypatch.setenv("EVENT_CONTEXT_WRITTEN", "true")
    monkeypatch.setenv("EVENT_CONTEXT_DAILY_CAP", "1")
    get_settings.cache_clear()
    writer = ScriptedModel([{"phrase": PHRASE}])

    with_calendar(monkeypatch, meeting(MEETING, at=moment + timedelta(minutes=20)))
    first = await run_tick(moment, model=writer)

    with_calendar(
        monkeypatch,
        meeting(MEETING, at=moment + timedelta(minutes=25), event_id="event-второй"),
    )
    second = await run_tick(moment + timedelta(minutes=5), model=writer)

    assert PHRASE in contexts(first)[0]
    assert writer.calls == 1, "второй вызов должен быть отсечён потолком"
    assert len(contexts(second)) == 1
    assert PHRASE not in contexts(second)[0]
    assert "резюме" in contexts(second)[0]
