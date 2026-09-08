"""Что означает отказ Google Calendar и что из этого следует.

Поверх `core/provider_errors`, а не вместо него: правило ADR-0014 — «временный
отказ не останавливает, настроечный останавливает» — здесь то же самое,
и `transient()` переиспользуется как есть. Своим тут стало ровно две вещи,
и обе — про то, чего у моделей нет.

**Первая: `403` — это два разных отказа под одним кодом.** У Gemini `403`
означает «ключ не принят», и общий `advice()` так и советует. У календаря
`403` приходит и на исчерпанную квоту (`rateLimitExceeded` — пройдёт к утру
сам), и на нехватку прав (`insufficientPermissions` — не пройдёт никогда,
областей у токена столько, сколько их выдали при авторизации). Один совет
на оба был бы неправдой ровно в половине случаев, а остановка воркера
из-за квотного `403` — это повторение 20 августа 2026, когда из-за `429`
на эмбеддингах встали расшифровка и напоминания.

**Вторая: `invalid_grant`.** Он приходит с кодом `400`, то есть неотличим
от «мы прислали плохой запрос», и означает при этом отзыв refresh token —
самый дорогой отказ этой интеграции. ADR-0016 записал причину заранее:
пока приложение в статусе «Testing», Google отзывает токен **через семь
суток**, и бот молча перестаёт видеть календарь через неделю после настройки.
Поэтому он настроечный, громкий, и совет называет причину, а не отправляет
ждать.

Общий `provider_errors.advice()` при этом **не правится**: он про
`GOOGLE_API_KEY`, ключ AI Studio, который к календарю отношения не имеет
вовсе. Совет, отправивший владельца править не ту строку в `.env`, хуже
отсутствия совета.
"""

from collections.abc import Mapping
from typing import Any

from brain.core.provider_errors import ProviderError, transient

# Куда отсылать за процедурой. Совет обязан совпадать с инструкцией, а не
# пересказывать её по-своему: два расходящихся объяснения одного и того же
# хуже одного.
SETUP_DOC = "docs/google_calendar_setup.md"

# Причины `403`, которые проходят сами. Список короткий намеренно: всё,
# что в него не попало, считается настроечным — молчаливый старт при непонятном
# отказе возвращает слепоту, ради которой заведён `core/provider_errors`.
QUOTA_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})

# Списка «причины, означающие нехватку прав» здесь намеренно нет. Он был
# написан и удалён 07.09.2026, не дожив до первого вызова: разбор `403` устроен
# так, что всё не попавшее в `QUOTA_REASONS` уже считается настроечным,
# и второй список ничего не решал бы, а только выглядел бы решающим. Мёртвая
# константа в этом проекте — начало того же почерка, что `loops.is_stuck`.


class CalendarError(ProviderError):
    """Отказ Google Calendar вместе с кодом ответа.

    База для всех остальных. Как и у моделей, код едет объектом, а не строкой
    внутри сообщения: без него нельзя ни отличить квоту от нехватки прав,
    ни дать верный совет.
    """


class CalendarTransientError(CalendarError):
    """Пройдёт само: квота, частота, сбой на стороне Google.

    `fatal` переопределён, потому что базовое правило считает его по коду,
    а квотный `403` по коду неотличим от отказа в правах. Здесь решение уже
    принято разбором тела, и код его больше не перерешивает.
    """

    @property
    def fatal(self) -> bool:
        return False


class CalendarSetupError(CalendarError):
    """Не пройдёт: права, учётные данные, отозванный токен.

    Повторять бессмысленно — ответ не изменится, пока владелец не сходит
    в консоль Google или в `.env`.
    """

    @property
    def fatal(self) -> bool:
        return True


class TokenExpiredError(CalendarTransientError):
    """Access token протух. Лечится обновлением, а не правкой настроек.

    Временный намеренно: это рабочее состояние, а не поломка. Клиент обновляет
    токен и повторяет запрос **ровно один раз**; если и повтор отвечает `401`,
    отказ доезжает до вызывающего этим же классом — но уже как факт, а не
    как повод крутиться дальше.
    """


class CalendarForbiddenError(CalendarSetupError):
    """Выданных прав не хватает на этот вызов.

    Ожидаемый и **правильный** ответ на попытку записи в личный календарь
    владельца: `calendar.app.created` даёт доступ только к тому календарю,
    который приложение создало само. Живая проверка этого отказа входит
    в критерий приёмки фазы (ADR-0016, цена решения, п. 5).
    """


class InvalidGrantError(CalendarSetupError):
    """Refresh token отозван. Нужна повторная авторизация с ноутбука."""


class InvalidClientError(CalendarSetupError):
    """Клиент не найден или секрет не тот. Опечатка в `.env`."""


def from_api_response(status: int, payload: object, text: str) -> CalendarError:
    """Разобрать отказ Calendar API.

    Тело у него вида `{"error": {"code": …, "message": …, "errors": [{"reason": …}]}}`.
    Причина живёт в `errors[].reason` и решает судьбу `403` — по коду он
    неотличим от отказа в правах.
    """
    message = _api_message(payload) or text[:300]
    reason = _api_reason(payload)
    where = f"HTTP {status}. {message}" if message else f"HTTP {status}"

    if status == 401:
        return TokenExpiredError(f"календарь: {where}", status=status)

    if status == 403:
        if reason in QUOTA_REASONS:
            return CalendarTransientError(f"календарь: {where}", status=status)
        # Неизвестная причина `403` тоже считается настроечной: ждать отказа,
        # который не пройдёт, дороже, чем лишний раз сказать вслух.
        return CalendarForbiddenError(f"календарь: {where}", status=status)

    if transient(status):
        return CalendarTransientError(f"календарь: {where}", status=status)

    return CalendarSetupError(f"календарь: {where}", status=status)


def from_token_response(status: int, payload: object, text: str) -> CalendarError:
    """Разобрать отказ точки обмена токенов.

    Формат у неё **другой**, чем у Calendar API: `error` здесь строка
    (`invalid_grant`, `invalid_client`), а не объект. Разбирать их одним кодом
    значит потерять `invalid_grant` — самый дорогой отказ этой интеграции.
    """
    code = _oauth_error(payload)
    description = _oauth_description(payload) or text[:300]
    where = f"HTTP {status}. {code or ''} {description}".strip()

    if code == "invalid_grant":
        return InvalidGrantError(f"обновление токена календаря: {where}", status=status)

    if code in ("invalid_client", "unauthorized_client"):
        return InvalidClientError(f"обновление токена календаря: {where}", status=status)

    if transient(status):
        return CalendarTransientError(f"обновление токена календаря: {where}", status=status)

    return CalendarSetupError(f"обновление токена календаря: {where}", status=status)


def offline(error: Exception) -> CalendarError:
    """Ответа не было вовсе: сеть, таймаут, оборванное соединение.

    Настроечный по базовому правилу `provider_errors.transient(None)` — выбор
    в пользу громкого отказа. Для календаря это безопасно: громко здесь значит
    «строка в логе и `check_available` вернул «недоступен»», а не остановка.
    """
    return CalendarError(f"календарь не ответил: {error}")


def advice(error: CalendarError) -> str:
    """Что владельцу делать с этим отказом.

    Совет зависит от разобранного отказа, а не только от кода: `403` квоты
    и `403` прав требуют противоположных действий — подождать и сходить
    в консоль Google.
    """
    if isinstance(error, InvalidGrantError):
        return (
            "Refresh token отозван — календарь перестал отвечать совсем.\n"
            "Причин ровно три, и первая самая частая:\n"
            "  1. приложение осталось в статусе «Testing» — Google отзывает "
            "токен через семь суток;\n"
            "  2. вы сменили пароль Google;\n"
            "  3. доступ отозван в настройках аккаунта "
            "(myaccount.google.com/permissions).\n"
            f"Что делать: {SETUP_DOC}, шаг 7 (опубликовать приложение), "
            "затем заново `just calendar-auth` с ноутбука."
        )

    if isinstance(error, InvalidClientError):
        return (
            "Google не узнал OAuth-клиента. Проверьте GOOGLE_CALENDAR_CLIENT_ID "
            "и GOOGLE_CALENDAR_CLIENT_SECRET в .env — это не ключ AI Studio, "
            f"а отдельные учётные данные.\nОткуда они берутся: {SETUP_DOC}, шаг 6."
        )

    if isinstance(error, CalendarForbiddenError):
        return (
            "Выданных прав не хватает на этот вызов. Само это не пройдёт: "
            "областей доступа у токена столько, сколько их было в момент "
            "авторизации.\n"
            "Если это была запись в личный календарь — так и задумано "
            "(ADR-0016): система пишет только в свой календарь «Brain».\n"
            f"Если нет — сверьте три области доступа: {SETUP_DOC}, шаг 4, "
            "и авторизуйтесь заново."
        )

    if isinstance(error, TokenExpiredError):
        return (
            "Access token протух, и обновление его не починило. Обычно проходит "
            "само; если повторяется — токен отозван, и нужен повторный "
            f"`just calendar-auth` ({SETUP_DOC}, шаг 8)."
        )

    if error.status == 403 or error.status == 429:
        return (
            "Кончилась квота обращений к Calendar API. Проходит само; "
            "лимиты видны в консоли Google, раздел APIs & Services → Quotas."
        )

    if not error.fatal:
        return "Google отвечает ошибкой на своей стороне. Обычно проходит само; повторите позже."

    return (
        "Календарь не отвечает так, как мы ждём. Проверьте четыре переменные "
        "GOOGLE_CALENDAR_* в .env; процедура целиком — " + SETUP_DOC + "."
    )


def _mapping(payload: object) -> Mapping[str, Any] | None:
    return payload if isinstance(payload, Mapping) else None


def _api_message(payload: object) -> str:
    body = _mapping(payload)
    if body is None:
        return ""
    error = body.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message", ""))
    return str(error) if error else ""


def _api_reason(payload: object) -> str:
    """Причина отказа в нижнем регистре. Пустая строка означает «не назвали»."""
    body = _mapping(payload)
    if body is None:
        return ""
    error = body.get("error")
    if not isinstance(error, Mapping):
        return ""

    reasons = error.get("errors")
    if isinstance(reasons, list):
        for item in reasons:
            if isinstance(item, Mapping) and item.get("reason"):
                return str(item["reason"]).lower()

    # Новый формат Google кладёт сюда `PERMISSION_DENIED`. Он грубее старого
    # (квоту от прав не отличает), поэтому смотрится вторым, а не первым.
    return str(error.get("status", "")).lower()


def _oauth_error(payload: object) -> str:
    body = _mapping(payload)
    if body is None:
        return ""
    error = body.get("error")
    return error.lower() if isinstance(error, str) else ""


def _oauth_description(payload: object) -> str:
    body = _mapping(payload)
    if body is None:
        return ""
    return str(body.get("error_description", ""))
