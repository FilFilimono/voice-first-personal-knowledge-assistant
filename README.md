# Brain

Персональный ассистент с памятью. Принимает мысли голосом и текстом в Telegram,
размечает их моделью, хранит в Postgres и умеет искать по архиву.

Один пользователь, один сервер. Замысел — `docs/vision.md`, технический план —
`docs/tech_plan.md`, принятые решения — `docs/adr/`.

## Состояние

Фаза 0 — скелет. Работает: Postgres с pgvector, миграции, `/health`.
Бота ещё нет — он приходит в Фазе 1.

## Запуск

Нужны [uv](https://docs.astral.sh/uv/), [Docker](https://www.docker.com/)
и [just](https://github.com/casey/just).

```bash
cp .env.example .env      # заполнять пока нечего: у всего есть рабочие значения
just install              # зависимости
just up                   # Postgres
just migrate              # миграции
just dev                  # API на http://localhost:8000
```

Проверка:

```bash
curl localhost:8000/health
# {"status":"ok","database":"ok"}
```

## Разработка

```bash
just check      # ruff + mypy + быстрые тесты — то же, что гоняет CI
just test-all   # плюс тесты, которым нужен живой Postgres
```
