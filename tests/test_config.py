"""Что должно работать:

1. приложение стартует, когда заведена только база — ключи Фаз 1+ необязательны
2. whitelist читается из `123,456`, а не только из JSON
3. пустой whitelist означает «никого», а не «всех»
4. мусорное значение в whitelist падает при старте, а не молча пропускается
"""

import pytest
from pydantic import ValidationError

from brain.config import Settings


def _settings() -> Settings:
    """Настройки без чтения `.env`: тест не должен зависеть от машины."""
    return Settings(_env_file=None)


def test_starts_without_phase_one_keys() -> None:
    settings = _settings()

    assert settings.brain_env == "dev"
    assert settings.telegram_bot_token is None
    assert settings.google_api_key is None
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_allowed_user_ids_parsed_from_comma_separated_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456")

    assert _settings().telegram_allowed_user_ids == [123, 456]


def test_allowed_user_ids_default_to_nobody() -> None:
    assert _settings().telegram_allowed_user_ids == []


def test_allowed_user_ids_reject_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,вася")

    with pytest.raises(ValidationError):
        _settings()
