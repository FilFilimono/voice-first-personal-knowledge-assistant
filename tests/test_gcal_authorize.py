"""Что должно работать:

1. областей доступа ровно три, и это те самые три, что в инструкции владельцу.
   Расхождение здесь означало бы, что владелец согласовал в браузере одно,
   а система просит другое, — и узнал бы он об этом отказом `403` через неделю
2. `code_challenge` — это `S256` от `code_verifier` в base64url **без набивки**.
   Набивка не косметика: RFC 7636 требует её отсутствия, и Google отвергает
   вариант с ней
3. в адресе авторизации есть `access_type=offline` и `prompt=consent`. Без
   первого Google не выдаёт refresh token вовсе, без второго — не выдаёт его
   повторно уже согласившемуся владельцу, и строки для `.env` взять неоткуда
4. редирект — loopback, а не публичный адрес: `http://127.0.0.1:<порт>`
5. повторный запуск **не заводит второй календарь «Brain»**: скрипт запускают
   заново при отзыве токена, а календарь с событиями к этому моменту уже есть
6. напечатанные строки называются ровно теми именами, что стоят в `.env`

**Про то, чего здесь нет.** Браузер, слушатель на 127.0.0.1 и живой обмен кода
не проверяются: это разовая процедура, которую владелец исполняет руками
и видит своими глазами. Проверяется то, что при этом легко испортить молча
и незаметно, — набор прав, PKCE и то, что повторный запуск не плодит
календари.

**Про даты.** Настоящих часов этот модуль не спрашивает нигде, поэтому их
здесь и нет.
"""

import base64
import hashlib
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from brain.core.ports import CalendarEvent, CalendarRef
from brain.integrations.gcal import authorize

# Те же три строки, что в `docs/google_calendar_setup.md`, шаг 4. Выписаны
# здесь заново, а не импортированы: тест, сравнивающий значение сам с собой,
# не поймает подмену.
EXPECTED_SCOPES = (
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)


class _Calendars:
    """Календарь ровно в том объёме, который нужен `ensure_calendar`."""

    def __init__(self, existing: Sequence[CalendarRef]) -> None:
        self.existing = list(existing)
        self.created: list[str] = []

    async def calendars(self) -> Sequence[CalendarRef]:
        return self.existing

    async def create_calendar(self, *, title: str, timezone: str) -> CalendarRef:
        self.created.append(title)
        made = CalendarRef(id=f"новый::{title}", title=title, primary=False)
        self.existing.append(made)
        return made

    async def events(self, *, since: datetime, until: datetime) -> Sequence[CalendarEvent]:
        raise AssertionError("не нужен этому тесту")

    async def create_event(
        self,
        *,
        calendar_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str,
    ) -> CalendarEvent:
        raise AssertionError("не нужен этому тесту")


# --- Права ---


def test_the_scopes_are_exactly_the_three_from_the_instructions() -> None:
    """Ни одной лишней: владелец согласовывает то, что просит система."""
    assert authorize.SCOPES == EXPECTED_SCOPES


def test_no_scope_can_write_to_the_owners_own_calendars() -> None:
    """Запись есть ровно у `app.created`, и она про свой календарь."""
    assert not [scope for scope in authorize.SCOPES if scope.endswith("/auth/calendar")]
    assert not [scope for scope in authorize.SCOPES if scope.endswith("calendar.events")]


# --- PKCE ---


def test_the_challenge_is_s256_of_the_verifier_without_padding() -> None:
    keys = authorize.pkce()

    expected = base64.urlsafe_b64encode(hashlib.sha256(keys.verifier.encode()).digest())
    assert keys.challenge == expected.decode().rstrip("=")
    assert "=" not in keys.challenge


def test_every_run_gets_its_own_verifier() -> None:
    assert authorize.pkce().verifier != authorize.pkce().verifier


# --- Адрес авторизации ---


def test_the_authorization_url_asks_for_offline_access_and_consent() -> None:
    """Без обоих параметров refresh token не приходит, и процедура впустую."""
    url = authorize.authorization_url(
        client_id="брайн.apps.googleusercontent.com",
        redirect_uri="http://127.0.0.1:53412",
        challenge="вызов",
        state="состояние",
    )

    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["вызов"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [" ".join(EXPECTED_SCOPES)]


def test_the_redirect_is_a_loopback_address() -> None:
    """Публичного адреса у нас нет и не будет — `tech_plan.md` §2, врезка."""
    url = authorize.authorization_url(
        client_id="брайн",
        redirect_uri="http://127.0.0.1:53412",
        challenge="вызов",
        state="состояние",
    )

    (redirect,) = parse_qs(urlparse(url).query)["redirect_uri"]
    assert urlparse(redirect).hostname == "127.0.0.1"
    assert urlparse(redirect).scheme == "http"


# --- Повторный запуск ---


async def test_a_second_run_reuses_the_calendar_instead_of_making_another() -> None:
    """Отзыв токена — это повторный запуск, а не повод завести второй «Brain»."""
    calendars = _Calendars([CalendarRef(id="brain@group", title="Brain", primary=False)])

    found = await authorize.ensure_calendar(
        calendars,
        title="Brain",
        timezone="Europe/Rome",
    )

    assert found.id == "brain@group"
    assert calendars.created == []


async def test_the_first_run_creates_the_calendar() -> None:
    calendars = _Calendars([CalendarRef(id="личный", title="Личный", primary=True)])

    made = await authorize.ensure_calendar(
        calendars,
        title="Brain",
        timezone="Europe/Rome",
    )

    assert calendars.created == ["Brain"]
    assert made.title == "Brain"


# --- Что печатается владельцу ---


def test_the_printed_lines_use_the_names_that_live_in_env() -> None:
    """Строку вставляют копированием: имя в ней обязано совпадать с настройкой."""
    lines = authorize.env_lines(refresh_token="1//0gрефреш", calendar_id="brain@group")

    assert "GOOGLE_CALENDAR_REFRESH_TOKEN=1//0gрефреш" in lines
    assert "GOOGLE_CALENDAR_TARGET_ID=brain@group" in lines
