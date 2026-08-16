"""Конфигурация приложения.

Единственное место, где читается окружение. Секретов в коде нет — инвариант 6.

С Фазы 1 приложение обязано падать на старте, если не заданы токен бота или
ключ Google: молча стартовавший процесс без бота выглядит живым и отлаживается
часами. Ошибка называет переменные по именам, а не показывает трейс pydantic.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConfigError(RuntimeError):
    """Окружение задано неполно или неверно. Поднимается один раз, при старте."""


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

    # Таймзона владельца. Нужна напоминаниям: «завтра в 10» — это десять утра
    # там, где он живёт, а не в UTC, в котором живёт сервер.
    brain_timezone: str = "Europe/Moscow"

    # --- База ---
    # Драйвер только асинхронный: синхронного подключения в проекте нет нигде,
    # включая Alembic.
    database_url: str = "postgresql+asyncpg://brain:brain@localhost:5432/brain"

    # --- Telegram ---
    # Обязателен: без него бот не поднимется, и узнать об этом надо на старте.
    telegram_bot_token: str = ""

    # Whitelist отправителей. Пустой список означает «никого не пускаем»:
    # безопасный дефолт важнее удобства, забытая переменная не должна
    # незаметно открыть бота всему миру. Бот предупреждает об этом в логе —
    # молчащий бот и бот, который всех игнорирует, снаружи неотличимы.
    #
    # NoDecode отключает разбор значения как JSON — переменная задаётся
    # человеком в виде `123,456`, а не `[123, 456]`.
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Google AI Studio ---
    # Один ключ на два назначения: расшифровка голоса (Фаза 1) и эмбеддинги (Фаза 2).
    google_api_key: str = ""

    # --- Расшифровка голоса (ADR-0007, ADR-0009) ---
    stt_provider: Literal["gemini"] = "gemini"
    # Версия указывается явно. Алиасы вида `gemini-flash-lite-latest` провайдер
    # переставляет на другую модель без предупреждения — вместе с ними уезжает
    # поведение промпта, и архив становится неоднородным незаметно для нас.
    # Имя модели протухает за месяцы: это пункт квартальной проверки.
    stt_model: str = "gemini-3.5-flash-lite"
    # 15 минут — потолок из ADR-0007. Длиннее бот не принимает: резка по паузам
    # добавляет свои ошибки на стыках, и делать её до замера не за чем.
    stt_max_duration_sec: int = 900

    # --- Аудио ---
    # Каталог рядом с репозиторием, а не в нём: файлы голосовых в git не место.
    audio_dir: Path = Path("var/audio")
    # Окно на случай смены STT-модели и перепрогона (ADR-0007). Текст — вечно.
    audio_retention_days: int = 30

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, value: object) -> object:
        """Разобрать `123,456` в список чисел."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


# Переменные, без которых приложение не работает, и объяснение зачем каждая.
# Проверяются отдельно от pydantic: пустая строка для него валидна, а для нас нет.
REQUIRED: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "токен бота у @BotFather — без него бот не поднимется",
    "GOOGLE_API_KEY": "ключ Google AI Studio — им расшифровывается голос",
}


def _check_required(settings: Settings) -> None:
    """Упасть, назвав незаполненные переменные по именам."""
    missing = [
        f"  {name} — {why}"
        for name, why in REQUIRED.items()
        if not getattr(settings, name.lower()).strip()
    ]
    if missing:
        raise ConfigError(
            "Не заданы обязательные переменные окружения:\n"
            + "\n".join(missing)
            + "\n\nЗаполнить в .env (образец — .env.example)."
        )


@lru_cache
def get_settings() -> Settings:
    """Настройки процесса. Кэшируются: окружение за время жизни не меняется.

    Обязательные ключи здесь не проверяются намеренно: этой функцией пользуются
    Alembic и тесты, которым нужен только `database_url`. Требовать токен бота
    ради `alembic upgrade head` — способ научить всех держать в окружении
    фальшивый токен, после чего проверка перестанет что-либо значить.
    """
    try:
        return Settings()
    except ValidationError as error:
        raise ConfigError(f"Окружение задано неверно:\n{error}") from error


def get_runtime_settings() -> Settings:
    """Настройки для долгоживущего процесса: бота и воркера.

    Единственная точка, где проверяются обязательные ключи. Оба процесса
    вызывают её первой строкой и падают с внятным текстом, если чего-то нет:
    бот, который поднялся без токена, выглядит работающим и отлаживается часами.

    Наружу ошибка уходит как `SystemExit`, а не как исключение: над трейсбеком
    на двадцать кадров сообщение «заполните .env» теряется, а это единственное,
    что нужно прочитать.
    """
    try:
        settings = get_settings()
        _check_required(settings)
    except ConfigError as error:
        raise SystemExit(f"\n{error}\n") from None
    return settings
