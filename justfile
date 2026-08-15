# Команды проекта. Всё запускается через uv — отдельной активации venv не нужно.

default:
    @just --list

# Установить зависимости
install:
    uv sync

# Поднять Postgres (единственный сервис)
up:
    docker compose up -d
    docker compose ps

# Остановить Postgres. Данные остаются: -v здесь нет намеренно.
down:
    docker compose down

# Логи базы
logs:
    docker compose logs -f postgres

# Запустить API локально
dev:
    uv run uvicorn brain.api.app:app --reload --port 8000

# Быстрые тесты: без БД, без сети. Это же гоняет Stop-хук.
test:
    uv run pytest

# Все тесты, включая интеграционные (нужен поднятый Postgres)
test-all:
    uv run pytest -m ""

# Линт и форматирование
lint:
    uv run ruff format --check .
    uv run ruff check .

# Починить то, что чинится автоматически
fix:
    uv run ruff format .
    uv run ruff check --fix .

# Типы
typecheck:
    uv run mypy

# Всё, что гоняет CI
check: lint typecheck test

# Накатить миграции
migrate:
    uv run alembic upgrade head

# Новая миграция: just migration "описание"
migration message:
    uv run alembic revision -m "{{message}}"

# Текущая версия схемы
migration-status:
    uv run alembic current
