"""Разовая авторизация в Google Calendar. Запускается `just calendar-auth`.

**Один раз в жизни проекта, с ноутбука, а не на сервере.** Loopback-редирект
должен попасть в браузер, а на VPS браузера нет. Наружу отсюда уходят две
строки для `.env`; в работе этот модуль не исполняется никогда.

**Почему не публичный редирект и не FastAPI.** Клиент типа Desktop
редиректит на `http://127.0.0.1:<свободный порт>` — временный слушатель
на этой же машине. Порт в консоли Google не регистрируется. Ручной перенос
кода (OOB) Google отключил, custom URI scheme не рекомендует. Разбор — врезка
«Исправление 7 сентября 2026» в `docs/tech_plan.md` §2: до неё в плане стояло,
что OAuth Google требует нашей HTTP-ручки, и это было неверно.

**Почему всё руками, без `google-auth-oauthlib`.** ADR-0016, ось 1: библиотека
ради одноразового скрипта тянет `requests` в то же окружение, откуда стартует
воркер. «Только для разработки» — соглашение о том, кто её зовёт, а не
изоляция.

**Секреты в файлы не пишутся** (инвариант 6). Скрипт печатает готовые строки,
вставляет их владелец.
"""

import asyncio
import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass

import httpx

from brain.config import Settings, get_settings
from brain.core.ports import Calendar, CalendarRef
from brain.integrations.gcal import errors
from brain.integrations.gcal.auth import (
    TOKEN_ENDPOINT,
    Credentials,
    TokenSource,
    default_client_factory,
)
from brain.integrations.gcal.client import GoogleCalendar

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

# Три области доступа, и ровно эти (ADR-0016, ось 2). Записи в личные
# календари владельца среди них нет намеренно: `calendar.app.created` даёт
# доступ только к тому календарю, который приложение создало само.
#
# Список обязан совпадать с шагом 4 инструкции. Расхождение здесь означало бы,
# что владелец согласовал в браузере одно, а система просит другое, — и узнал
# бы он об этом отказом `403` через неделю.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)

# Что видит браузер после успеха. Коротко и по-русски: это последнее, что
# владелец увидит от процедуры, и оно должно сказать, куда возвращаться.
DONE_PAGE = """<!doctype html><meta charset="utf-8">
<title>Brain</title>
<body style="font-family: system-ui; padding: 3rem; max-width: 32rem">
<h1>Готово</h1>
<p>Вкладку можно закрыть. Две строки для <code>.env</code> напечатаны
в терминале, откуда вы запускали <code>just calendar-auth</code>.</p>
</body>"""


@dataclass(frozen=True, slots=True)
class Pkce:
    """Пара PKCE: секрет остаётся у нас, производная уходит в браузер.

    Нужна потому, что код авторизации летит через адресную строку браузера,
    то есть через среду, которую мы не контролируем. Перехвативший код без
    `code_verifier` обменять его не сможет.
    """

    verifier: str
    challenge: str


def pkce() -> Pkce:
    """Секрет и его `S256`-производная, обе в base64url без набивки.

    Набивка (`=`) снимается не для красоты: RFC 7636 требует именно
    base64url-without-padding, и Google отвергает вариант с ней.
    """
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return Pkce(verifier=verifier, challenge=challenge)


def authorization_url(*, client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    """Адрес, который открывается в браузере владельца."""
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # Без `offline` Google не выдаёт refresh token вовсе, и вся процедура
        # оказывается разовой в худшем смысле: доступ живёт час.
        "access_type": "offline",
        # Refresh token приходит только при явном согласии. Без `consent`
        # повторная авторизация уже согласившегося владельца вернёт один
        # access token, и строки для `.env` будет неоткуда взять.
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(query)}"


def env_lines(*, refresh_token: str, calendar_id: str) -> str:
    """Готовые строки для `.env` — ровно в том виде, в каком их вставляют."""
    return f"GOOGLE_CALENDAR_REFRESH_TOKEN={refresh_token}\nGOOGLE_CALENDAR_TARGET_ID={calendar_id}"


async def ensure_calendar(calendar: Calendar, *, title: str, timezone: str) -> CalendarRef:
    """Календарь «Brain»: найти уже заведённый или создать.

    **Повторный запуск не должен плодить второй «Brain».** Скрипт запускают
    заново при отзыве токена, и календарь с событиями к этому моменту уже есть.

    Совпадение ищется по названию, и у этого есть известная дыра: календарь
    с тем же именем, заведённый владельцем руками, сюда тоже попадёт, а писать
    в него система не сможет — `calendar.app.created` даёт доступ только
    к своим. Поэтому инструкция (шаг 0) прямо запрещает заводить его руками,
    а найденный календарь называется вслух, а не подставляется молча.
    """
    for existing in await calendar.calendars():
        if existing.title == title:
            return existing
    return await calendar.create_calendar(title=title, timezone=timezone)


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Слушатель на 127.0.0.1, который ловит один редирект и умирает."""

    code: str | None = None
    state: str | None = None
    failure: str | None = None

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = _first(query, "code")
        _Catcher.state = _first(query, "state")
        _Catcher.failure = _first(query, "error")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DONE_PAGE.encode())

    def log_message(self, format: str, *args: object) -> None:
        """Тишина: своей строкой доступа процедура ничего не объясняет."""


async def _exchange(*, settings: Settings, code: str, verifier: str, redirect_uri: str) -> str:
    """Код авторизации в refresh token. Один POST, тот же, что у обновления."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": settings.google_calendar_client_id,
        "client_secret": settings.google_calendar_client_secret,
    }
    async with default_client_factory() as client:
        response = await client.post(TOKEN_ENDPOINT, data=form)

    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    if response.status_code != httpx.codes.OK:
        raise errors.from_token_response(response.status_code, body, response.text)

    token = body.get("refresh_token")
    if not token:
        # Самая частая причина — повторная авторизация без `prompt=consent`.
        # Сказать это прямо дешевле, чем оставить владельца с пустой строкой.
        raise errors.CalendarSetupError(
            "Google вернул доступ без refresh token. Обычно это значит, что "
            "согласие уже было дано раньше: отзовите доступ на "
            "myaccount.google.com/permissions и повторите."
        )
    return str(token)


def main() -> None:
    settings = get_settings()
    if not (settings.google_calendar_client_id and settings.google_calendar_client_secret):
        raise SystemExit(
            "Не заданы GOOGLE_CALENDAR_CLIENT_ID и GOOGLE_CALENDAR_CLIENT_SECRET.\n"
            f"Откуда они берутся: {errors.SETUP_DOC}, шаг 6."
        )

    keys = pkce()
    state = secrets.token_urlsafe(16)

    # Порт выбирает система, а не мы: у Desktop-клиента он в консоли Google
    # не регистрируется, и занятый «любимый» порт сорвал бы процедуру.
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"

    url = authorization_url(
        client_id=settings.google_calendar_client_id,
        redirect_uri=redirect_uri,
        challenge=keys.challenge,
        state=state,
    )

    print("Открываю браузер. Если он не открылся — ссылка:\n")
    print(url, "\n")
    print("На красном экране «Google hasn't verified this app» это ожидаемо:")
    print("  Advanced → Go to Brain (unsafe). Разбор — " + errors.SETUP_DOC + ", шаг 8.\n")

    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.handle_request()
    server.server_close()

    if _Catcher.failure:
        raise SystemExit(f"Google отказал: {_Catcher.failure}")
    if _Catcher.state != state:
        # Не педантизм: несовпадение означает, что редирект пришёл не от нашего
        # запроса, и код из него менять нельзя.
        raise SystemExit("Ответ пришёл не на наш запрос (state не совпал). Повторите.")
    if not _Catcher.code:
        raise SystemExit("Google не прислал код авторизации. Повторите.")

    asyncio.run(_finish(settings, code=_Catcher.code, keys=keys, redirect_uri=redirect_uri))


async def _finish(settings: Settings, *, code: str, keys: Pkce, redirect_uri: str) -> None:
    refresh_token = await _exchange(
        settings=settings, code=code, verifier=keys.verifier, redirect_uri=redirect_uri
    )

    calendar = GoogleCalendar(
        tokens=TokenSource(
            Credentials(
                client_id=settings.google_calendar_client_id,
                client_secret=settings.google_calendar_client_secret,
                refresh_token=refresh_token,
            )
        ),
        timezone=settings.brain_timezone,
    )

    existing = {ref.title for ref in await calendar.calendars()}
    target = await ensure_calendar(
        calendar, title=settings.google_calendar_name, timezone=settings.brain_timezone
    )

    if settings.google_calendar_name in existing:
        print(
            f"Календарь «{target.title}» уже существовал — второй не создаю.\n"
            "Если вы заводили его руками, удалите тот, что создан руками: система "
            "может писать только в свой.\n"
        )
    else:
        print(f"Календарь «{target.title}» создан.\n")

    print("Готово. Две строки в .env:\n")
    print(env_lines(refresh_token=refresh_token, calendar_id=target.id))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


if __name__ == "__main__":
    main()
