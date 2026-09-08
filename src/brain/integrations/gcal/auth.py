"""Access token для Google Calendar: получение, срок годности, проверка при старте.

Весь OAuth в рабочем режиме — **один POST**. Это и есть довод, по которому
ADR-0016 выбрал `httpx` вместо клиента Google: `google-auth-oauthlib` притащил
бы `requests`, то есть синхронный HTTP-клиент внутрь async-воркера, ради
запроса, который помещается в двадцать строк.

**Токен обновляется заранее, а не по факту `401`.** Разница не в аккуратности,
а в цене: полагаться на отказ как на рабочий механизм значит платить одним
лишним отказом за каждый час работы, и в журнале это выглядит как будто
что-то ломается. `401` при этом всё равно обрабатывается — но как страховка
на случай, когда Google отозвал токен раньше срока.

**Срок годности считается по монотонным часам, а не по календарной дате.**
Это единственное место в проекте, где так, и причина конкретная: `expires_in`
приходит от Google длительностью, а не моментом, и переводить её в дату значит
завязаться на то, что часы сервера не переставят. Монотонные часы к этому
безразличны, а сравнивать нам надо только «прошло ли достаточно».

**`check_available` ничего не бросает, и это отличие от расшифровки,
эмбеддингов и разметки.** Там настроечный отказ обязан валить старт (ADR-0014),
здесь — нет: без расшифровки системы нет, а без календаря захват записи,
разметка, поиск и напоминания работают целиком. Уронить воркер из-за
незаданного `GOOGLE_CALENDAR_REFRESH_TOKEN` значило бы повторить 20 августа
2026, когда из-за квоты на эмбеддинги встали расшифровка и напоминания.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from brain.config import Settings
from brain.integrations.gcal import errors

# Точка обмена и обновления токенов. Отдельная от адреса Calendar API —
# у них разный формат тела отказа, и разбирают их разные функции
# (`errors.from_token_response` против `errors.from_api_response`).
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# За сколько секунд до конца срока токен считается протухшим.
#
# Две минуты, а не десять секунд: между решением «токен ещё жив» и приходом
# ответа лежит целый запрос к Google, и в него надо уместиться с запасом,
# иначе выигранная секунда возвращается лишним `401`.
REFRESH_MARGIN_SEC = 120

# Сколько ждём ответа. Календарь — не тот механизм, ради которого стоит
# держать зависший запрос: тик касаний идёт раз в пять минут, и запрос,
# живущий дольше, гарантированно пересекается со следующим тиком.
TIMEOUT_SEC = 20.0

# Как собирается HTTP-клиент. Шов вынесен параметром ровно затем, чтобы тесты
# подставляли `httpx.MockTransport` и не ходили в сеть, — как у `core/llm`
# и `core/stt`.
ClientFactory = Callable[[], httpx.AsyncClient]


def default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SEC)


@dataclass(frozen=True, slots=True)
class Credentials:
    """Учётные данные OAuth-клиента типа Desktop.

    `client_secret` у такого клиента выдаётся и участвует в обмене — вопреки
    распространённому «у desktop-клиентов секрета нет». Он не является тайной
    в криптографическом смысле (приложение раздаётся вместе с ним), но Google
    его спрашивает, и без него обмен не проходит.
    """

    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class CalendarStatus:
    """Что известно про календарь на момент старта.

    Три состояния, а не два, и это важно для `/status`: «не настроен»
    и «настроен, но не отвечает» — разные новости. Первое означает, что
    владелец не дошёл до конца инструкции; второе — что дошёл, а потом что-то
    отвалилось, и почти всегда это отозванный refresh token.
    """

    configured: bool
    available: bool
    detail: str


class TokenSource:
    """Access token с кэшем на время жизни процесса.

    Кэш здесь не оптимизация: без него каждый вызов к календарю стоил бы двух
    запросов вместо одного, а тик касаний ходит в календарь раз в пять минут
    круглые сутки.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        client_factory: ClientFactory = default_client_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._client_factory = client_factory
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def token(self) -> str:
        """Живой access token. Обновляет, если срок подходит к концу."""
        if self._token is not None and self._clock() < self._expires_at - REFRESH_MARGIN_SEC:
            return self._token
        return await self._refresh()

    def invalidate(self) -> None:
        """Забыть текущий токен.

        Шов для клиента, поймавшего `401`: Google отозвал токен раньше
        объявленного срока, и наш кэш врёт. Метод отдельный, а не «обновись
        сам», чтобы решение о повторе принимал тот, кто знает, сколько повторов
        уже было, — иначе получается круг.
        """
        self._token = None
        self._expires_at = 0.0

    async def _refresh(self) -> str:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._credentials.refresh_token,
            "client_id": self._credentials.client_id,
            "client_secret": self._credentials.client_secret,
        }

        try:
            async with self._client_factory() as client:
                response = await client.post(TOKEN_ENDPOINT, data=form)
        except httpx.HTTPError as failure:
            raise errors.offline(failure) from failure

        if response.status_code != httpx.codes.OK:
            raise errors.from_token_response(response.status_code, _json(response), response.text)

        body = _json(response)
        if not isinstance(body, dict) or not body.get("access_token"):
            # Ответ без токена — это не «пустой ответ», а сломанный протокол.
            # Молча вернуть пустую строку значило бы уронить следующий запрос
            # там, где причина уже не видна.
            raise errors.CalendarSetupError(
                "обновление токена календаря: ответ без access_token",
                status=response.status_code,
            )

        self._token = str(body["access_token"])
        # `expires_in` Google присылает всегда, но полагаться на это незачем:
        # час — то, что он отдаёт на практике, и ошибка в меньшую сторону стоит
        # одного лишнего обновления, а в большую — часа отказов.
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        return self._token


async def check_available(
    settings: Settings,
    *,
    client_factory: ClientFactory = default_client_factory,
) -> CalendarStatus:
    """Отвечает ли календарь. Не бросает никогда — см. докстринг модуля."""
    missing = _missing_settings(settings)
    if missing:
        return CalendarStatus(
            configured=False,
            available=False,
            detail=(
                "Календарь не настроен: не заданы "
                + ", ".join(missing)
                + f". Процедура целиком — {errors.SETUP_DOC}."
            ),
        )

    source = TokenSource(
        Credentials(
            client_id=settings.google_calendar_client_id,
            client_secret=settings.google_calendar_client_secret,
            refresh_token=settings.google_calendar_refresh_token,
        ),
        client_factory=client_factory,
    )

    try:
        await source.token()
    except errors.CalendarError as refusal:
        return CalendarStatus(configured=True, available=False, detail=errors.advice(refusal))

    return CalendarStatus(configured=True, available=True, detail="")


def _missing_settings(settings: Settings) -> list[str]:
    """Каких переменных не хватает — по именам, как их видит владелец в `.env`.

    Имена, а не «календарь настроен неполно»: первый вопрос по такому сообщению
    всегда «а какой именно строки нет», и ответ на него должен быть в самом
    сообщении.
    """
    required = {
        "GOOGLE_CALENDAR_CLIENT_ID": settings.google_calendar_client_id,
        "GOOGLE_CALENDAR_CLIENT_SECRET": settings.google_calendar_client_secret,
        "GOOGLE_CALENDAR_REFRESH_TOKEN": settings.google_calendar_refresh_token,
        "GOOGLE_CALENDAR_TARGET_ID": settings.google_calendar_target_id,
    }
    return [name for name, value in required.items() if not value.strip()]


def _json(response: httpx.Response) -> object:
    """Тело ответа, если это вообще JSON.

    Google отвечает HTML на часть отказов шлюза, и `response.json()` на нём
    бросает — то есть отказ провайдера превратился бы в нашу ошибку разбора,
    а причина потерялась бы.
    """
    try:
        return response.json()
    except ValueError:
        return None
