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
# `--stale` — перепрогон разметки, сделанной до ADR-0012: у неё нет смысловых
# единиц, а из них выводятся дела Фазы 4. Нужна живая модель.
reenrich *args:
    uv run python -m brain.maintenance reenrich {{args}}

# Дела по уже размеченным единицам — без вызова модели. Разовая операция
# при переходе на Фазу 4: разметка до неё про дела ничего не знала.
backfill-commitments *args:
    uv run python -m brain.maintenance backfill-commitments {{args}}

# Названия открытым делам и петлям. Нужна живая модель, пачками по 25.
# `--dry-run` печатает цену: сколько названий нужно и во сколько запросов это
# обойдётся. `--preview N` отвечает на другой вопрос — что получится: берёт
# одну пачку из N дел (по умолчанию 10, это один вызов), зовёт модель
# по-настоящему, печатает названия рядом с отрезками и не пишет в базу ничего.
# `--recompute` пересчитывает и уже проставленные — без него смена TITLES_MODEL
# оставила бы старые названия навсегда. `--limit` делит прогон на части:
# суточный лимит бесплатного тира — 20 запросов на модель.
backfill-titles *args:
    uv run python -m brain.maintenance backfill-titles {{args}}

# Прогон eval классификации: accuracy, матрица ошибок, доля ошибок среди
# «уверен». Разметку набора делает владелец — иначе меряем модель моделью.
eval-classification *args:
    uv run python -m evals.classification.run {{args}}

# Прогон eval-набора ретривала: recall@5, MRR и recall по каналам.
# Без числа отсюда Фаза 2 не закрыта.
eval-retrieval *args:
    uv run python -m evals.retrieval.run {{args}}

# Замер чистки расшифровки — **по одной записи**, как чистит воркер. Пачками
# мерить нельзя: на пачке модель видит соседние записи. Печатает медиану и p95
# латентности: из них выбирается CLEANUP_WAIT_SEC. Пока не прогнан, CLEANUP_MODEL
# — дефолт, а не выбор (ADR-0015).
eval-cleanup *args:
    uv run python -m evals.cleanup.run {{args}}

# Повтор касаний по прошедшим дням живого архива: что ушло бы и что промолчало.
# Ничего не пишет и не отправляет. Оценку 1–5 ставит владелец.
eval-digest *args:
    uv run python -m evals.digest.replay {{args}}

# Границы смысловых единиц: сколько отрезков нельзя назвать, потому что предмет
# действия остался в соседней единице («надо её начинать читать»). Считается
# **кодом по базе, без единого вызова модели** — это запрос, а не замер модели.
# Базовая линия до правки промпта 27.08.2026: 11 отрезков из 72.
eval-units *args:
    uv run python -m evals.units.run {{args}}

# Замер расшифровки: WER **и отдельно доля искажённых имён собственных**.
# Вторая метрика важнее первой и заведена не для полноты: общий WER бывает
# приемлемым при полностью испорченных именах, а имя ложится в `transcripts`
# — таблицу, которая не правится, — и уезжает в разметку, чанки, поиск и дела.
# Ключ --engine сравнивает движки **на одном корпусе**: Gemini против
# локального. Корпус размечает владелец, дословно, руками.
eval-stt *args:
    uv run python -m evals.stt.run {{args}}

# Разовая авторизация в Google Calendar (ADR-0016). Делается **один раз
# и с ноутбука**: посреди процедуры открывается браузер, а на сервере браузера
# нет. Печатает две строки для .env — вставляет их владелец, секреты в файлы
# проект не пишет. Процедура целиком: docs/google_calendar_setup.md.
calendar-auth:
    uv run python -m brain.integrations.gcal.authorize

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
