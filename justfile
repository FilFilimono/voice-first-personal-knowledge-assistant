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

# Бот: приём сообщений. Подтверждение приёма должно приходить быстро,
# поэтому медленная работа живёт в отдельном процессе — see `just worker`.
bot:
    uv run python -m brain.adapters.telegram

# Воркер: расшифровка, напоминания, ретеншен аудио.
worker:
    uv run pgq run brain.worker:create_app

# Адрес базы для CLI pgqueuer: он подключается напрямую через asyncpg
# и про приставку `+asyncpg` в DATABASE_URL ничего не знает.
pgdsn := "$(uv run python -c 'from brain.storage.db import asyncpg_dsn; print(asyncpg_dsn())')"

# Упавшие задания, оставшиеся в очереди: посмотреть и вернуть в работу
failed:
    PGDSN="{{pgdsn}}" uv run pgq failed

requeue *ids:
    PGDSN="{{pgdsn}}" uv run pgq requeue {{ids}}

# Записи без расшифровки, у которых задания в очереди уже нет.
# Сначала посмотреть, потом ставить.
requeue-transcripts *args:
    uv run python -m brain.maintenance requeue-transcripts {{args}}

# Записи, которых нет в поиске: нет чанков, нет вектора, либо вектор есть
# у приватной. Список считается по данным, а не по журналу очереди.
reindex *args:
    uv run python -m brain.maintenance reindex {{args}}

# Записи без разметки. Список считается по данным, а не по журналу очереди.
reenrich *args:
    uv run python -m brain.maintenance reenrich {{args}}

# Прогон eval классификации: accuracy, матрица ошибок, доля ошибок среди
# «уверен». Разметку набора делает владелец — иначе меряем модель моделью.
eval-classification *args:
    uv run python -m evals.classification.run {{args}}

# Прогон eval-набора ретривала: recall@5, MRR и recall по каналам.
# Без числа отсюда Фаза 2 не закрыта.
eval-retrieval *args:
    uv run python -m evals.retrieval.run {{args}}

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

# Схема очереди соответствует версии библиотеки (ADR-0008, шаг 4)
verify-queue:
    PGDSN="{{pgdsn}}" uv run pgq verify --expect present
