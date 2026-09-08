"""Что должно работать:

1. `403` — это **два разных отказа под одним кодом**, и путать их нельзя:
   `rateLimitExceeded` проходит сам, `insufficientPermissions` не пройдёт
   никогда. Первый — предупреждение, второй — громкая остановка с советом
2. `401` — протухший access token, а не сломанная настройка: он лечится
   обновлением и потому считается временным
3. `429` и пятисотые — временные, как и во всём остальном проекте
4. `invalid_grant` при обновлении токена — **настроечный** отказ, и совет
   называет все три причины, по которым он бывает: статус «Testing» и семь
   суток, смена пароля Google, отзыв доступа в настройках аккаунта
5. совет отсылает к `docs/google_calendar_setup.md`, шаг 7, и не советует
   править `GOOGLE_API_KEY` — это другой ключ другого механизма
6. неизвестный отказ считается настроечным: молчаливый старт при непонятном
   отказе возвращает ту слепоту, ради которой заведён `provider_errors`

**Про пункт 1.** Это главный тест файла. `403` с квотой проходит к утру сам,
и остановка воркера из-за него — та же ошибка, что 20 августа 2026 с `429`
на эмбеддингах. `403` с нехваткой прав не пройдёт ни к утру, ни через неделю:
областей доступа у токена столько, сколько их было в момент авторизации,
и лечится это повторной авторизацией, а не ожиданием.

**Про пункт 4.** Цена ошибки записана в ADR-0016: пока приложение в статусе
«Testing», Google отзывает refresh token через семь суток. Бот проработает
неделю и молча перестанет видеть календарь — тот самый почерк, которым
в проекте прячутся дыры. Совет обязан назвать причину, а не отправить в ретраи.
"""

from typing import Any

from brain.integrations.gcal import errors

# Тела, которыми Google отвечает на календарные вызовы. Формат старый
# (`error.errors[].reason`), он же и приходит сегодня.
RATE_LIMITED: dict[str, Any] = {
    "error": {
        "errors": [
            {
                "domain": "usageLimits",
                "reason": "rateLimitExceeded",
                "message": "Rate Limit Exceeded",
            }
        ],
        "code": 403,
        "message": "Rate Limit Exceeded",
    }
}

USER_RATE_LIMITED: dict[str, Any] = {
    "error": {
        "errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded"}],
        "code": 403,
        "message": "User Rate Limit Exceeded",
    }
}

NO_PERMISSION: dict[str, Any] = {
    "error": {
        "errors": [{"domain": "global", "reason": "insufficientPermissions"}],
        "code": 403,
        "message": "Insufficient Permission",
    }
}

FORBIDDEN: dict[str, Any] = {
    "error": {
        "errors": [{"domain": "global", "reason": "forbidden"}],
        "code": 403,
        "message": "Forbidden",
    }
}

EXPIRED: dict[str, Any] = {
    "error": {
        "errors": [{"domain": "global", "reason": "authError"}],
        "code": 401,
        "message": "Invalid Credentials",
    }
}

# Тело обмена/обновления токена. Формат у него **другой**, чем у Calendar API:
# `error` здесь строка, а не объект, — и разбирать его надо отдельно.
INVALID_GRANT: dict[str, Any] = {
    "error": "invalid_grant",
    "error_description": "Token has been expired or revoked.",
}

BAD_CLIENT: dict[str, Any] = {
    "error": "invalid_client",
    "error_description": "The OAuth client was not found.",
}


# --- 403: квота против нехватки прав ---


def test_rate_limited_403_is_transient() -> None:
    """Квота проходит к утру сама. Останавливаться из-за неё — ошибка 20.08.2026."""
    refusal = errors.from_api_response(403, RATE_LIMITED, "")

    assert refusal.fatal is False
    assert refusal.status == 403


def test_user_rate_limited_403_is_transient_too() -> None:
    refusal = errors.from_api_response(403, USER_RATE_LIMITED, "")

    assert refusal.fatal is False


def test_missing_permission_403_is_a_setup_failure() -> None:
    """Нехватка прав не пройдёт никогда: областей у токена столько, сколько выдали."""
    refusal = errors.from_api_response(403, NO_PERMISSION, "")

    assert refusal.fatal is True
    assert isinstance(refusal, errors.CalendarForbiddenError)


def test_plain_forbidden_403_is_a_setup_failure() -> None:
    assert errors.from_api_response(403, FORBIDDEN, "").fatal is True


def test_the_two_kinds_of_403_get_different_advice() -> None:
    """Один совет на оба `403` был бы неправдой ровно в половине случаев."""
    quota = errors.advice(errors.from_api_response(403, RATE_LIMITED, ""))
    denied = errors.advice(errors.from_api_response(403, NO_PERMISSION, ""))

    assert quota != denied
    assert "права" in denied.lower() or "област" in denied.lower()


def test_advice_never_sends_the_owner_to_the_ai_studio_key() -> None:
    """`GOOGLE_API_KEY` — ключ AI Studio, у календаря он ни при чём.

    Общий `provider_errors.advice` на `403` советует именно его, поэтому
    у календаря своя лестница советов, а общая не правится.
    """
    for refusal in (
        errors.from_api_response(403, NO_PERMISSION, ""),
        errors.from_api_response(401, EXPIRED, ""),
        errors.from_token_response(400, INVALID_GRANT, ""),
    ):
        assert "GOOGLE_API_KEY" not in errors.advice(refusal)


# --- 401 и прочие временные ---


def test_expired_access_token_is_transient_and_named() -> None:
    """`401` лечится обновлением токена, а не правкой `.env`."""
    refusal = errors.from_api_response(401, EXPIRED, "")

    assert isinstance(refusal, errors.TokenExpiredError)
    assert refusal.fatal is False


def test_429_and_5xx_are_transient() -> None:
    for status in (429, 500, 502, 503, 504):
        assert errors.from_api_response(status, {}, "").fatal is False, status


def test_unknown_refusal_is_treated_as_a_setup_failure() -> None:
    """Молчаливый старт при непонятном отказе — возврат к слепоте августа 2026."""
    assert errors.from_api_response(418, {}, "чайник").fatal is True


def test_the_provider_explanation_reaches_the_message() -> None:
    """Объяснение Google обычно и есть ответ на вопрос «что делать»."""
    assert "Insufficient Permission" in str(errors.from_api_response(403, NO_PERMISSION, ""))


# --- invalid_grant ---


def test_invalid_grant_is_a_setup_failure_not_a_retry() -> None:
    refusal = errors.from_token_response(400, INVALID_GRANT, "")

    assert isinstance(refusal, errors.InvalidGrantError)
    assert refusal.fatal is True


def test_invalid_grant_advice_names_all_three_reasons() -> None:
    """Причин ровно три, и первая — та, о которой владелец не догадается сам."""
    text = errors.advice(errors.from_token_response(400, INVALID_GRANT, ""))

    assert "Testing" in text
    assert "семь суток" in text or "семи суток" in text
    assert "пароль" in text
    assert "отозв" in text


def test_invalid_grant_advice_points_at_the_setup_document() -> None:
    """Совет обязан совпадать с инструкцией, а не пересказывать её по-своему."""
    text = errors.advice(errors.from_token_response(400, INVALID_GRANT, ""))

    assert "docs/google_calendar_setup.md" in text
    assert "шаг 7" in text


def test_a_broken_client_is_a_setup_failure_as_well() -> None:
    """`invalid_client` — это опечатка в `.env`, и ждать её прохождения нечего."""
    refusal = errors.from_token_response(401, BAD_CLIENT, "")

    assert refusal.fatal is True
    assert "GOOGLE_CALENDAR_CLIENT_ID" in errors.advice(refusal)


def test_token_endpoint_5xx_is_still_transient() -> None:
    """Сбой на стороне Google при обновлении токена проходит сам."""
    assert errors.from_token_response(503, {}, "").fatal is False
