"""Календарь владельца через Calendar API. Реализация порта `ports.Calendar`.

Инвариант 2 в редакции ADR-0016: прямые HTTP-вызовы Google живут только здесь.

**Четыре вызова, и список несимметричный намеренно.** Читать система может все
календари, писать — только в тот, который создала сама: такие права выбраны
в ADR-0016 и это ограничение фазы, а не недоделка. Правки и удаления события
здесь нет потому, что `calendar.app.created` их не даст, и метод, всегда
отвечающий `403`, был бы обещанием, которое некому выполнить.

**Три места, где легко проехать молча, и все три под тестом.**

1. **Пагинация.** У владельца может быть больше событий, чем влезает
   в страницу, и потерянный хвост неотличим от пустого дня. Читаем до конца
   `nextPageToken`, а не первую страницу.
2. **События «на весь день».** Google отдаёт их полем `date`, без времени.
   Приводить их надо к границам суток **в таймзоне владельца**: полночь UTC
   уносит однодневное событие на соседний день, и в утренней сводке это
   выглядит не ошибкой, а чужим расписанием. Сдвиг на фиксированные два часа
   не годится — зимой у Рима смещение другое.
3. **`401`.** Ровно одно обновление токена и ровно один повтор. Круг был бы
   хуже отказа: он молчит до таймаута и не называет причину.
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from brain.config import Settings
from brain.core.ports import CalendarEvent, CalendarRef
from brain.integrations.gcal import errors
from brain.integrations.gcal.auth import (
    ClientFactory,
    Credentials,
    TokenSource,
    default_client_factory,
)

API = "https://www.googleapis.com/calendar/v3"

# Сколько страниц готовы прочитать за один заход. Потолок нужен не от жадности
# Google, а от нашей ошибки: цикл по `nextPageToken` без границы при неверном
# разборе ответа крутится вечно и молча. Двадцать страниц — это тысячи событий,
# то есть заведомо больше, чем бывает у одного человека за окно в две недели.
MAX_PAGES = 20


class GoogleCalendar:
    """Порт `ports.Calendar` поверх httpx."""

    def __init__(
        self,
        *,
        tokens: TokenSource,
        timezone: str,
        client_factory: ClientFactory = default_client_factory,
    ) -> None:
        self._tokens = tokens
        self._timezone = ZoneInfo(timezone)
        self._client_factory = client_factory

    async def calendars(self) -> Sequence[CalendarRef]:
        items = await self._paged(f"{API}/users/me/calendarList", params={})
        return [
            CalendarRef(
                id=str(item["id"]),
                title=str(item.get("summary", "")),
                primary=bool(item.get("primary", False)),
            )
            for item in items
            if item.get("id")
        ]

    async def events(self, *, since: datetime, until: datetime) -> Sequence[CalendarEvent]:
        collected: list[CalendarEvent] = []
        for calendar in await self.calendars():
            raw = await self._paged(
                f"{API}/calendars/{calendar.id}/events",
                params={
                    # Развёрнутые вхождения, а не правило повторения: и сводка,
                    # и контекст перед событием отвечают на вопрос «что
                    # сегодня», а не «как часто».
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeMin": since.isoformat(),
                    "timeMax": until.isoformat(),
                },
            )
            collected.extend(
                self._event(item, calendar)
                for item in raw
                # Отменённая встреча в сводке — это встреча, которой нет.
                if item.get("status") != "cancelled" and item.get("id")
            )
        return collected

    async def create_calendar(self, *, title: str, timezone: str) -> CalendarRef:
        body = await self._request(
            "POST", f"{API}/calendars", json={"summary": title, "timeZone": timezone}
        )
        return CalendarRef(
            id=str(body["id"]),
            title=str(body.get("summary", title)),
            # Вторичный календарь основным не бывает по построению.
            primary=False,
        )

    async def create_event(
        self,
        *,
        calendar_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str,
    ) -> CalendarEvent:
        body = await self._request(
            "POST",
            f"{API}/calendars/{calendar_id}/events",
            json={
                "summary": title,
                "description": description,
                "start": {"dateTime": starts_at.astimezone(UTC).isoformat()},
                "end": {"dateTime": ends_at.astimezone(UTC).isoformat()},
            },
        )
        # Календарь берётся из того, куда писали, а не из ответа: в ответе
        # он лежит в `organizer`, и на вторичном календаре это то же самое,
        # а на будущем расширении прав (ADR-0016) — уже нет.
        organizer = body.get("organizer")
        name = str(organizer.get("displayName", "")) if isinstance(organizer, Mapping) else ""
        return self._event(body, CalendarRef(id=calendar_id, title=name, primary=False))

    # --- Разбор ---

    def _event(self, item: Mapping[str, Any], calendar: CalendarRef) -> CalendarEvent:
        start, all_day = self._moment(item.get("start"))
        end, _ = self._moment(item.get("end"))
        attendees = item.get("attendees")
        return CalendarEvent(
            id=str(item["id"]),
            calendar_id=calendar.id,
            calendar_title=calendar.title,
            title=str(item.get("summary", "")),
            starts_at=start,
            ends_at=end,
            all_day=all_day,
            location=str(item["location"]) if item.get("location") else None,
            attendees=tuple(
                str(one["email"])
                for one in (attendees if isinstance(attendees, list) else [])
                if isinstance(one, Mapping) and one.get("email")
            ),
        )

    def _moment(self, raw: object) -> tuple[datetime, bool]:
        """Момент события в UTC и признак «на весь день».

        Две ветки, потому что Google отдаёт два разных поля: `dateTime` у события
        со временем и `date` у события на весь день. Вторая — та самая, где
        полночь UTC даёт правдоподобный и неверный результат.
        """
        if not isinstance(raw, Mapping):
            # Ни `dateTime`, ни `date` — событие, которого мы не понимаем.
            # Выдумывать ему время нельзя: оно попадёт в сводку как настоящее.
            raise errors.CalendarSetupError("календарь: у события нет ни dateTime, ни date")

        moment = raw.get("dateTime")
        if moment:
            return datetime.fromisoformat(str(moment)).astimezone(UTC), False

        day = raw.get("date")
        if day:
            midnight = datetime.combine(
                date.fromisoformat(str(day)), time.min, tzinfo=self._timezone
            )
            return midnight.astimezone(UTC), True

        raise errors.CalendarSetupError("календарь: у события нет ни dateTime, ни date")

    # --- Транспорт ---

    async def _paged(self, url: str, *, params: dict[str, str]) -> list[Mapping[str, Any]]:
        """Все страницы ответа, а не первая."""
        items: list[Mapping[str, Any]] = []
        page: str | None = None

        for _ in range(MAX_PAGES):
            body = await self._request(
                "GET", url, params=params if page is None else {**params, "pageToken": page}
            )
            items.extend(_items(body))
            page = str(body["nextPageToken"]) if body.get("nextPageToken") else None
            if page is None:
                return items

        # Дошли до потолка и токен всё ещё есть. Молчать нельзя: это либо
        # у владельца невероятный календарь, либо мы неверно читаем ответ,
        # и оба случая надо увидеть, а не получить обрезанный список.
        raise errors.CalendarSetupError(
            f"календарь: {MAX_PAGES} страниц подряд и конец списка не наступил ({url})"
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Один вызов API. `401` даёт ровно одно обновление токена и один повтор."""
        response = await self._send(method, url, params=params, json=json)

        if response.status_code == httpx.codes.UNAUTHORIZED:
            # Google отозвал токен раньше объявленного срока. Кэш врёт —
            # выбрасываем его и пробуем ещё раз, **один**.
            self._tokens.invalidate()
            response = await self._send(method, url, params=params, json=json)

        if response.status_code != httpx.codes.OK:
            raise errors.from_api_response(response.status_code, _body(response), response.text)

        body = _body(response)
        if not isinstance(body, Mapping):
            raise errors.CalendarSetupError(
                f"календарь: ответ не разбирается как объект ({url})",
                status=response.status_code,
            )
        return body

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        token = await self._tokens.token()
        try:
            async with self._client_factory() as client:
                return await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as failure:
            raise errors.offline(failure) from failure


def build_calendar(settings: Settings) -> GoogleCalendar:
    """Собрать клиента из настроек.

    Отдельной функцией, а не конструктором: `GoogleCalendar` не должен знать
    про `Settings` — иначе тест на разбор ответа пришлось бы начинать
    с построения окружения.
    """
    return GoogleCalendar(
        tokens=TokenSource(
            Credentials(
                client_id=settings.google_calendar_client_id,
                client_secret=settings.google_calendar_client_secret,
                refresh_token=settings.google_calendar_refresh_token,
            )
        ),
        timezone=settings.brain_timezone,
    )


def _body(response: httpx.Response) -> object:
    """Тело, если это JSON. Google отвечает HTML на часть отказов шлюза."""
    try:
        return response.json()
    except ValueError:
        return None


def _items(body: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    raw = body.get("items")
    if not isinstance(raw, list):
        return
    for item in raw:
        if isinstance(item, Mapping):
            yield item
