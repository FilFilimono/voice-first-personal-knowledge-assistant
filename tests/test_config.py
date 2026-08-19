"""Что должно работать:

1. процесс с ботом падает на старте, если не задан токен или ключ Google,
   и в ошибке названы обе переменные — а не показан трейс pydantic
2. миграциям и тестам хватает одного `database_url`: обязательные ключи
   проверяются только там, где без них нельзя
3. whitelist читается из `123,456`, а не только из JSON
4. пустой whitelist означает «никого», а не «всех»
5. мусорное значение в whitelist падает при старте, а не молча пропускается
"""

from collections.abc import Iterator

import pytest

from brain.config import ConfigError, Settings, get_runtime_settings, get_settings


def _settings(**overrides: object) -> Settings:
    """Настройки без чтения `.env`: тест не должен зависеть от машины."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Окружение машины не должно влиять на результат, а кэш — протекать между тестами."""
    for name in ("TELEGRAM_BOT_TOKEN", "GOOGLE_API_KEY", "TELEGRAM_ALLOWED_USER_IDS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_runtime_start_fails_and_names_every_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())

    # SystemExit, а не ConfigError: над трейсбеком сообщение «заполните .env»
    # теряется, а прочитать надо именно его.
    with pytest.raises(SystemExit) as error:
        get_runtime_settings()

    message = str(error.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "GOOGLE_API_KEY" in message
    assert ".env" in message


def test_runtime_start_fails_when_only_one_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brain.config.get_settings", lambda: _settings(telegram_bot_token="123:x"))

    with pytest.raises(SystemExit) as error:
        get_runtime_settings()

    message = str(error.value)
    assert "GOOGLE_API_KEY" in message
    assert "TELEGRAM_BOT_TOKEN" not in message


def test_runtime_start_passes_when_both_keys_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    filled = _settings(telegram_bot_token="123:x", google_api_key="AI-x")
    monkeypatch.setattr("brain.config.get_settings", lambda: filled)

    assert get_runtime_settings() is filled


def test_migrations_do_not_need_the_bot_token() -> None:
    """Alembic и интеграционные тесты берут только `database_url`."""
    settings = _settings()

    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.telegram_bot_token == ""


def test_allowed_user_ids_parsed_from_comma_separated_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456")

    assert Settings(_env_file=None).telegram_allowed_user_ids == [123, 456]


def test_allowed_user_ids_default_to_nobody() -> None:
    assert _settings().telegram_allowed_user_ids == []


def test_allowed_user_ids_reject_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,вася")

    with pytest.raises(ConfigError):
        get_settings()


def test_empty_quota_setting_means_unknown_not_a_crash() -> None:
    """`ENRICH_DAILY_QUOTA=` без значения — законный способ сказать «не знаю».

    Найдено при написании ADR-0014: пустая переменная роняла приложение
    на старте с трейсом pydantic. Мина того же рода, что чинилась в тот день,
    только с другой стороны — и попала бы она в `.env.example`, то есть
    к владельцу с первого запуска.
    """
    settings = Settings(_env_file=None, enrich_daily_quota="", embedding_daily_quota="  ")

    assert settings.enrich_daily_quota is None
    assert settings.embedding_daily_quota is None


def test_quota_setting_still_parses_a_number() -> None:
    settings = Settings(_env_file=None, embedding_daily_quota="1000")

    assert settings.embedding_daily_quota == 1000
