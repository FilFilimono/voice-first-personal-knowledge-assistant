"""Что должно работать:

1. access token берётся одним POST на `https://oauth2.googleapis.com/token`
   с `grant_type=refresh_token` — никакого SDK, никакого второго HTTP-клиента
2. токен держится в памяти процесса: два обращения подряд — один запрос
   к Google, а не два
3. обновляется **заранее, с запасом**, а не по факту `401`: `401` остаётся
   страховкой, а не рабочим механизмом
4. `invalid_grant` доезжает настроечным отказом, а не уходит в ретраи
5. `check_available()` при отсутствующих учётных данных отвечает «не настроено»
   и **ничего не бросает** — календарь не имеет права ронять воркер
6. `check_available()` при отозванном токене отвечает «недоступен» и называет
   причину, но воркер поднимается

**Про пункт 5.** Это отличие календаря от расшифровки, эмбеддингов и разметки,
и оно намеренное: без расшифровки системы нет, а без календаря захват записи,
разметка, поиск и напоминания работают целиком. Ронять воркер из-за незаданного
`GOOGLE_CALENDAR_REFRESH_TOKEN` значило бы повторить 20 августа 2026, когда
из-за квоты **на эмбеддинги** встали расшифровка и напоминания.

**Про время.** Часов настоящих здесь нет и быть не должно: срок годности токена
считается по `time.monotonic()`, а не по календарной дате, и в тест он приходит
параметром. Прибитое число секунд поэтому безопасно — правило проекта про даты
от настоящих часов работает там, где код сам зовёт `datetime.now()`.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from brain.config import Settings
from brain.integrations.gcal import auth, errors

Handler = Callable[[httpx.Request], httpx.Response]

FRESH: dict[str, Any] = {
    "access_token": "ya29.первый",
    "expires_in": 3599,
    "scope": "https://www.googleapis.com/auth/calendar.app.created",
    "token_type": "Bearer",
}

NEXT: dict[str, Any] = {"access_token": "ya29.второй", "expires_in": 3599, "token_type": "Bearer"}

REVOKED: dict[str, Any] = {
    "error": "invalid_grant",
    "error_description": "Token has been expired or revoked.",
}

CREDENTIALS = auth.Credentials(
    client_id="брайн.apps.googleusercontent.com",
    client_secret="секрет",
    refresh_token="1//0gрефреш",
)


def _transport(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _serving(bodies: list[dict[str, Any]], seen: list[httpx.Request]) -> Handler:
    """Отдаёт тела по очереди и запоминает, о чём спросили."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = bodies[min(len(seen) - 1, len(bodies) - 1)]
        return httpx.Response(200, json=body)

    return handler


def _refusing(status: int, payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


class _Clock:
    """Монотонные часы под управлением теста."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _configured() -> Settings:
    return _settings(
        google_calendar_client_id=CREDENTIALS.client_id,
        google_calendar_client_secret=CREDENTIALS.client_secret,
        google_calendar_refresh_token=CREDENTIALS.refresh_token,
        google_calendar_target_id="brain@group.calendar.google.com",
    )


# --- Обновление токена ---


async def test_the_refresh_is_one_post_with_a_grant_type() -> None:
    seen: list[httpx.Request] = []

    source = auth.TokenSource(CREDENTIALS, client_factory=_transport(_serving([FRESH], seen)))
    token = await source.token()

    assert token == "ya29.первый"
    assert len(seen) == 1
    assert str(seen[0].url) == auth.TOKEN_ENDPOINT
    assert seen[0].method == "POST"

    form = parse_qs(seen[0].content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [CREDENTIALS.refresh_token]
    assert form["client_id"] == [CREDENTIALS.client_id]
    assert form["client_secret"] == [CREDENTIALS.client_secret]


async def test_the_token_is_cached_for_the_life_of_the_process() -> None:
    """Второе обращение не обязано стоить второго запроса к Google."""
    seen: list[httpx.Request] = []
    source = auth.TokenSource(CREDENTIALS, client_factory=_transport(_serving([FRESH], seen)))

    assert await source.token() == await source.token()
    assert len(seen) == 1


async def test_the_token_is_refreshed_ahead_of_its_expiry() -> None:
    """Обновление **заранее**, а не по факту `401`.

    `401` остаётся страховкой: на нём тоже обновляемся, но полагаться на отказ
    как на рабочий механизм значит платить лишним отказом за каждый час работы.
    """
    seen: list[httpx.Request] = []
    clock = _Clock()
    source = auth.TokenSource(
        CREDENTIALS,
        client_factory=_transport(_serving([FRESH, NEXT], seen)),
        clock=clock,
    )

    assert await source.token() == "ya29.первый"

    # Ещё не истёк, но до конца осталось меньше запаса.
    clock.value += 3599 - auth.REFRESH_MARGIN_SEC + 1
    assert await source.token() == "ya29.второй"
    assert len(seen) == 2


async def test_invalidate_forces_exactly_one_more_refresh() -> None:
    """Шов, которым пользуется клиент, поймав `401`."""
    seen: list[httpx.Request] = []
    source = auth.TokenSource(CREDENTIALS, client_factory=_transport(_serving([FRESH, NEXT], seen)))

    await source.token()
    source.invalidate()

    assert await source.token() == "ya29.второй"
    assert len(seen) == 2


async def test_a_revoked_refresh_token_raises_a_setup_failure() -> None:
    source = auth.TokenSource(CREDENTIALS, client_factory=_transport(_refusing(400, REVOKED)))

    with pytest.raises(errors.InvalidGrantError) as refusal:
        await source.token()

    assert refusal.value.fatal is True


async def test_a_dead_network_is_a_calendar_error_not_a_raw_httpx_error() -> None:
    """Отказ обязан доехать до места решения объектом, а не строкой."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть отвалилась")

    source = auth.TokenSource(CREDENTIALS, client_factory=_transport(handler))

    with pytest.raises(errors.CalendarError) as refusal:
        await source.token()

    assert refusal.value.status is None


# --- Проверка при старте ---


async def test_check_available_says_not_configured_and_does_not_raise() -> None:
    """Незаданный календарь — не отказ провайдера, а «этой части просто нет»."""
    asked: list[httpx.Request] = []

    status = await auth.check_available(
        _settings(), client_factory=_transport(_serving([FRESH], asked))
    )

    assert status.configured is False
    assert status.available is False
    # В Google при этом не ходили вовсе: спрашивать нечем.
    assert asked == []


async def test_check_available_needs_the_target_calendar_too() -> None:
    """Клиент, который авторизовался и не знает, куда писать, настроен наполовину."""
    half = _settings(
        google_calendar_client_id=CREDENTIALS.client_id,
        google_calendar_client_secret=CREDENTIALS.client_secret,
        google_calendar_refresh_token=CREDENTIALS.refresh_token,
    )

    status = await auth.check_available(half, client_factory=_transport(_refusing(200, FRESH)))

    assert status.configured is False
    assert "GOOGLE_CALENDAR_TARGET_ID" in status.detail


async def test_check_available_confirms_a_working_token() -> None:
    status = await auth.check_available(
        _configured(), client_factory=_transport(_refusing(200, FRESH))
    )

    assert (status.configured, status.available) == (True, True)


async def test_check_available_reports_a_revoked_token_without_raising() -> None:
    """Воркер обязан подняться: без календаря захват записи работает."""
    status = await auth.check_available(
        _configured(), client_factory=_transport(_refusing(400, REVOKED))
    )

    assert (status.configured, status.available) == (True, False)
    assert "Testing" in status.detail
    assert "docs/google_calendar_setup.md" in status.detail


async def test_check_available_survives_a_transient_refusal() -> None:
    """Пятисотая у Google не должна выглядеть как сломанная настройка."""
    status = await auth.check_available(
        _configured(), client_factory=_transport(_refusing(503, {}))
    )

    assert status.available is False
    assert status.detail != ""
