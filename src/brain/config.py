"""Конфигурация приложения.

Единственное место, где читается окружение. Секретов в коде нет — инвариант 6.

Ключи Фаз 1+ объявлены как необязательные намеренно: приложение Фазы 0
обязано стартовать на машине, где заведена только база.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки, собранные из переменных окружения и `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Окружение ---
    brain_env: Literal["dev", "prod"] = "dev"

    # --- База ---
    # Драйвер только асинхронный: синхронного подключения в проекте нет нигде,
    # включая Alembic.
    database_url: str = "postgresql+asyncpg://brain:brain@localhost:5432/brain"

    # --- Telegram (задействуется с Фазы 1) ---
    telegram_bot_token: str | None = None

    # Whitelist отправителей. Пустой список означает «никого не пускаем»:
    # безопасный дефолт важнее удобства, забытая переменная не должна
    # незаметно открыть бота всему миру.
    #
    # NoDecode отключает разбор значения как JSON — переменная задаётся
    # человеком в виде `123,456`, а не `[123, 456]`.
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Google AI Studio (задействуется с Фазы 1) ---
    # Один ключ на два назначения: дешёвый тир и эмбеддинги.
    google_api_key: str | None = None

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, value: object) -> object:
        """Разобрать `123,456` в список чисел."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Настройки процесса. Кэшируются: окружение за время жизни не меняется."""
    return Settings()
