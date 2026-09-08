"""Что должно работать:

1. `401` даёт **ровно одно** обновление токена и **ровно один** повтор запроса.
   Не круг: протухший токен, который не чинится обновлением, обязан доехать
   до вызывающего отказом, а не крутиться до таймаута
2. пагинация обязательна и у списка календарей, и у событий: у владельца может
   быть больше событий, чем влезает в страницу, а молча потерянный хвост здесь
   неотличим от пустого дня
3. событие «на весь день» превращается в границы суток **в таймзоне владельца**,
   а не в полночь UTC
4. и превращается по-настоящему, а не сдвигом на фиксированные два часа:
   зимой у Рима смещение другое
5. событие со временем приезжает в UTC и aware
6. `calendar_title` едет вместе с событием: показывать событие и не знать,
   из какого оно календаря, — значит идти за именем отдельно и забыть
7. `events.list` спрашивает развёрнутые вхождения (`singleEvents=true`)
   и окно `timeMin`/`timeMax`
8. запись идёт в **названный** календарь, а не в тот, что зашит в коде
9. отказ Google доезжает объектом с кодом, а не строкой в тексте

**Про пункт 3.** Это ровно тот класс ошибки, за который проект уже платил:
однодневное событие, приведённое к полуночи UTC, уезжает на соседний день,
и в утренней сводке это выглядит не ошибкой, а чужим расписанием.

**Про даты в тесте.** Они прибиты намеренно и это безопасно: окно приходит
в `events()` параметрами, а границы суток считаются из строки `date`, которую
прислал Google. Настоящих часов код этого файла не спрашивает нигде — правило
проекта про дату от часов работает там, где код сам зовёт `datetime.now()`.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from brain.config import Settings
from brain.integrations.gcal import errors
from brain.integrations.gcal.auth import Credentials, TokenSource
from brain.integrations.gcal.client import GoogleCalendar, build_calendar

Handler = Callable[[httpx.Request], httpx.Response]

CREDENTIALS = Credentials(client_id="брайн", client_secret="секрет", refresh_token="1//0g")

PERSONAL = "vladelec@gmail.com"
BRAIN = "brain@group.calendar.google.com"

# Таймзона владельца по умолчанию. Летом Рим — UTC+2, зимой UTC+1.
TIMEZONE = "Europe/Rome"

NO_PERMISSION: dict[str, Any] = {
    "error": {
        "errors": [{"domain": "global", "reason": "insufficientPermissions"}],
        "code": 403,
        "message": "Insufficient Permission",
    }
}


def _page(pages: list[list[dict[str, Any]]], token: str | None) -> dict[str, Any]:
    """Страница по её номеру плюс токен следующей, если она есть."""
    index = 0 if token is None else int(token)
    body: dict[str, Any] = {"items": pages[index] if index < len(pages) else []}
    if index + 1 < len(pages):
        body["nextPageToken"] = str(index + 1)
    return body


class _Google:
    """Google в том объёме, который нужен клиенту: токен и четыре вызова.

    Сеть не участвует, разбор ответов настоящий — ровно как у остальных
    провайдеров проекта (`tests/test_llm_gemini.py`).
    """

    def __init__(
        self,
        *,
        calendars: list[list[dict[str, Any]]] | None = None,
        events: dict[str, list[list[dict[str, Any]]]] | None = None,
        unauthorized: int = 0,
    ) -> None:
        self.calendars = calendars if calendars is not None else [[]]
        self.events = events or {}
        # Сколько первых обращений к API отвечают «токен протух».
        self.unauthorized = unauthorized

        self.refreshes = 0
        self.api_calls: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.refreshes += 1
            return httpx.Response(
                200, json={"access_token": f"ya29.{self.refreshes}", "expires_in": 3599}
            )

        self.api_calls.append(request)
        if request.content:
            self.bodies.append(json.loads(request.content))

        if self.unauthorized > 0:
            self.unauthorized -= 1
            return httpx.Response(
                401, json={"error": {"code": 401, "message": "Invalid Credentials"}}
            )

        return self._route(request)

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        token = request.url.params.get("pageToken")

        if path == "/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json=_page(self.calendars, token))

        if path == "/calendar/v3/calendars" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"id": BRAIN, "summary": body["summary"], "timeZone": body["timeZone"]}
            )

        if path.endswith("/events") and request.method == "POST":
            calendar_id = path.removeprefix("/calendar/v3/calendars/").removesuffix("/events")
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "созданное",
                    "summary": body["summary"],
                    "start": body["start"],
                    "end": body["end"],
                    "organizer": {"email": calendar_id},
                },
            )

        if path.endswith("/events"):
            calendar_id = path.removeprefix("/calendar/v3/calendars/").removesuffix("/events")
            return httpx.Response(200, json=_page(self.events.get(calendar_id, [[]]), token))

        return httpx.Response(404, json={"error": {"code": 404, "message": f"нет пути {path}"}})


def _factory(google: Handler) -> Callable[[], httpx.AsyncClient]:
    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(google))

    return make


def _calendar(google: Handler, *, timezone: str = TIMEZONE) -> GoogleCalendar:
    factory = _factory(google)
    return GoogleCalendar(
        tokens=TokenSource(CREDENTIALS, client_factory=factory),
        timezone=timezone,
        client_factory=factory,
    )


def _listed(calendar_id: str, title: str, *, primary: bool = False) -> dict[str, Any]:
    return {"id": calendar_id, "summary": title, "primary": primary, "accessRole": "owner"}


def _timed(event_id: str, title: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **extra,
    }


WINDOW = {
    "since": datetime(2026, 9, 8, 0, 0, tzinfo=UTC),
    "until": datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
}


# --- Протухший токен ---


async def test_a_401_causes_exactly_one_refresh_and_one_retry() -> None:
    """Один повтор — это страховка. Два были бы кругом."""
    google = _Google(calendars=[[_listed(PERSONAL, "Личный", primary=True)]], unauthorized=1)

    result = await _calendar(google).calendars()

    assert [ref.id for ref in result] == [PERSONAL]
    assert len(google.api_calls) == 2, "один отказ, один повтор"
    assert google.refreshes == 2, "первый токен и одно обновление после 401"


async def test_a_401_that_does_not_heal_reaches_the_caller_instead_of_looping() -> None:
    """Круг был бы хуже отказа: он молчит до таймаута и не называет причину."""
    google = _Google(unauthorized=99)

    with pytest.raises(errors.TokenExpiredError):
        await _calendar(google).calendars()

    assert len(google.api_calls) == 2
    assert google.refreshes == 2


async def test_the_bearer_token_goes_into_the_header() -> None:
    google = _Google(calendars=[[_listed(PERSONAL, "Личный", primary=True)]])

    await _calendar(google).calendars()

    assert google.api_calls[0].headers["Authorization"] == "Bearer ya29.1"


# --- Пагинация ---


async def test_the_calendar_list_is_read_to_the_last_page() -> None:
    """Молча потерянный хвост здесь неотличим от «календарей больше нет»."""
    google = _Google(
        calendars=[
            [_listed(PERSONAL, "Личный", primary=True)],
            [_listed(BRAIN, "Brain")],
        ]
    )

    result = await _calendar(google).calendars()

    assert [ref.id for ref in result] == [PERSONAL, BRAIN]
    assert [ref.primary for ref in result] == [True, False]


async def test_events_are_read_to_the_last_page() -> None:
    """Две страницы дают объединённый список, а не первую страницу."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    _timed(
                        "первое", "Созвон", "2026-09-10T09:00:00+02:00", "2026-09-10T10:00:00+02:00"
                    )
                ],
                [
                    _timed(
                        "второе", "Обед", "2026-09-10T13:00:00+02:00", "2026-09-10T14:00:00+02:00"
                    )
                ],
            ]
        },
    )

    result = await _calendar(google).events(**WINDOW)

    assert [event.id for event in result] == ["первое", "второе"]


# --- Время ---


async def test_an_all_day_event_becomes_the_owners_day_boundaries() -> None:
    """Полночь UTC унесла бы однодневное событие на соседний день."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    {
                        "id": "отпуск",
                        "summary": "Выходной",
                        "start": {"date": "2026-09-10"},
                        "end": {"date": "2026-09-11"},
                    }
                ]
            ]
        },
    )

    (event,) = await _calendar(google).events(**WINDOW)

    # Рим в сентябре — UTC+2, поэтому сутки начинаются в 22:00 предыдущего дня.
    assert event.all_day is True
    assert event.starts_at == datetime(2026, 9, 9, 22, 0, tzinfo=UTC)
    assert event.ends_at == datetime(2026, 9, 10, 22, 0, tzinfo=UTC)


async def test_the_day_boundary_follows_the_owners_winter_offset_too() -> None:
    """Сдвиг на фиксированные два часа прошёл бы летний тест и соврал бы в январе."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    {
                        "id": "январь",
                        "summary": "Выходной",
                        "start": {"date": "2026-01-15"},
                        "end": {"date": "2026-01-16"},
                    }
                ]
            ]
        },
    )

    (event,) = await _calendar(google).events(
        since=datetime(2026, 1, 1, tzinfo=UTC), until=datetime(2026, 2, 1, tzinfo=UTC)
    )

    # Рим в январе — UTC+1.
    assert event.starts_at == datetime(2026, 1, 14, 23, 0, tzinfo=UTC)


async def test_a_timed_event_arrives_aware_and_in_utc() -> None:
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    _timed(
                        "созвон",
                        "Созвон",
                        "2026-09-10T09:00:00+02:00",
                        "2026-09-10T10:00:00+02:00",
                        location="Zoom",
                        attendees=[{"email": "кто-то@example.com"}, {"email": "ещё@example.com"}],
                    )
                ]
            ]
        },
    )

    (event,) = await _calendar(google).events(**WINDOW)

    assert event.starts_at == datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
    assert event.ends_at == datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    assert event.all_day is False
    assert event.location == "Zoom"
    assert event.attendees == ("кто-то@example.com", "ещё@example.com")


# --- Что и у кого спрашиваем ---


async def test_events_carry_the_calendar_they_came_from() -> None:
    """Имя календаря берётся оттуда же, откуда событие, а не отдельным походом."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True), _listed(BRAIN, "Brain")]],
        events={
            PERSONAL: [[_timed("личное", "Врач", "2026-09-10T09:00:00Z", "2026-09-10T10:00:00Z")]],
            BRAIN: [
                [_timed("наше", "Позвонить маме", "2026-09-10T18:00:00Z", "2026-09-10T18:15:00Z")]
            ],
        },
    )

    result = await _calendar(google).events(**WINDOW)

    assert {(event.calendar_id, event.calendar_title) for event in result} == {
        (PERSONAL, "Личный"),
        (BRAIN, "Brain"),
    }


async def test_events_are_asked_expanded_and_inside_the_window() -> None:
    """Правило повторения системе не нужно нигде: вопрос всегда «что сегодня»."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={PERSONAL: [[]]},
    )

    await _calendar(google).events(**WINDOW)

    params = google.api_calls[-1].url.params
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    assert params["timeMin"].startswith("2026-09-08T00:00:00")
    assert params["timeMax"].startswith("2026-09-20T00:00:00")


async def test_cancelled_events_are_dropped() -> None:
    """Отменённая встреча в сводке — это встреча, которой нет."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    _timed("живое", "Созвон", "2026-09-10T09:00:00Z", "2026-09-10T10:00:00Z"),
                    {"id": "отменённое", "status": "cancelled"},
                ]
            ]
        },
    )

    result = await _calendar(google).events(**WINDOW)

    assert [event.id for event in result] == ["живое"]


# --- Запись ---


async def test_create_calendar_asks_for_a_named_calendar_in_a_timezone() -> None:
    google = _Google()

    created = await _calendar(google).create_calendar(title="Brain", timezone=TIMEZONE)

    assert (created.id, created.title, created.primary) == (BRAIN, "Brain", False)
    assert google.bodies[-1] == {"summary": "Brain", "timeZone": TIMEZONE}


async def test_create_event_goes_into_the_named_calendar() -> None:
    """Календарь приходит параметром: ADR-0016 не обещает, что он ровно один."""
    google = _Google()

    event = await _calendar(google).create_event(
        calendar_id=BRAIN,
        title="Позвонить маме",
        starts_at=datetime(2026, 9, 10, 18, 0, tzinfo=UTC),
        ends_at=datetime(2026, 9, 10, 18, 15, tzinfo=UTC),
        description="из записи №41",
    )

    assert unquote(google.api_calls[-1].url.path) == f"/calendar/v3/calendars/{BRAIN}/events"
    assert event.calendar_id == BRAIN
    assert event.starts_at == datetime(2026, 9, 10, 18, 0, tzinfo=UTC)

    body = google.bodies[-1]
    assert body["summary"] == "Позвонить маме"
    assert body["description"] == "из записи №41"
    assert body["start"]["dateTime"].startswith("2026-09-10T18:00:00")


async def test_a_refused_write_reaches_the_caller_with_its_reason() -> None:
    """Живой `403` на личный календарь — критерий приёмки фазы (ADR-0016)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3599})
        return httpx.Response(403, json=NO_PERMISSION)

    with pytest.raises(errors.CalendarForbiddenError) as refusal:
        await _calendar(handler).create_event(
            calendar_id=PERSONAL,
            title="Не выйдет",
            starts_at=datetime(2026, 9, 10, 18, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 10, 18, 15, tzinfo=UTC),
            description="",
        )

    assert refusal.value.status == 403
    assert refusal.value.fatal is True


# --- Сборка из настроек ---


async def test_build_calendar_carries_the_settings_into_the_request() -> None:
    """Таймзона и учётные данные обязаны доехать от `.env` до самого вызова."""
    google = _Google(
        calendars=[[_listed(PERSONAL, "Личный", primary=True)]],
        events={
            PERSONAL: [
                [
                    {
                        "id": "отпуск",
                        "summary": "Выходной",
                        "start": {"date": "2026-09-10"},
                        "end": {"date": "2026-09-11"},
                    }
                ]
            ]
        },
    )
    calendar = build_calendar(
        Settings(
            _env_file=None,
            brain_timezone="Asia/Tokyo",
            google_calendar_client_id="брайн",
            google_calendar_client_secret="секрет",
            google_calendar_refresh_token="1//0g",
            google_calendar_target_id=BRAIN,
        )
    )
    # Шов подменяется после сборки: `build_calendar` про сеть ничего не знает,
    # а проверять надо именно то, что уехало Google.
    factory = _factory(google)
    calendar._client_factory = factory
    calendar._tokens._client_factory = factory

    (event,) = await calendar.events(**WINDOW)

    # Токио — UTC+9 круглый год: сутки начинаются в 15:00 предыдущего дня.
    assert event.starts_at == datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
