"""Версия схемы проверяется при старте — и это тест на путь, а не на функцию.

**Почему этот файл существует.** Дважды подряд код, требующий непринятой
миграции, падал стодвадцатистрочной трассировкой: 26 августа 2026 это уронило
`just backfill-titles` (`invalid input value for enum commitment_state`),
7 сентября — воркер (`relation "habits" does not exist`), причём **на каждом
тике, каждые пять минут**. Проверка вызовом при старте была у моделей и не была
у схемы, хотя цена ошибки та же: система выглядит рабочей и падает на первом
настоящем действии.

**Тест на функцию не заменяет теста на путь** — правило проекта, купленное
девятью дырами одного почерка. Поэтому быстрые тесты здесь проверяют только
текст сообщения, а главное проверяется интеграционно: **воркер, бот и команды
обслуживания действительно зовут проверку и действительно не стартуют.**
Проверить это можно единственным способом — накатить схему и откатить строку
`alembic_version` назад, то есть воспроизвести ровно то, что было 7 сентября.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.storage import schema

# Настоящая ревизия из истории проекта, предшествующая голове. Взята живой,
# а не выдумана: «отстала» и «неизвестна коду» — разные беды с разными
# советами, и различить их можно только на ревизии, которую Alembic знает.
STALE = "a71c9d0b4e26"

# Ревизии, которой в истории нет вовсе. Так выглядит база, ушедшая вперёд кода.
UNKNOWN = "ffffffffffff"


def test_expected_revision_is_the_alembic_head() -> None:
    """Код ждёт ту же голову, которую накатит `just migrate`.

    Иначе проверка была бы про себя саму: своя копия номера ревизии разошлась
    бы с историей молча, и старт валился бы на здоровой базе.
    """
    assert schema.expected_revision() == schema.script().get_current_head()


def test_message_names_both_revisions_and_the_command() -> None:
    """Одна внятная строка вместо ста двадцати строк трассировки."""
    said = schema.Mismatch(expected="b8e41c7d3a29", found=STALE).message

    assert STALE in said
    assert "b8e41c7d3a29" in said
    assert "just migrate" in said


def test_advice_differs_when_the_database_is_ahead_of_the_code() -> None:
    """База впереди кода — `just migrate` не поможет, и обещать это нельзя.

    Тот же почерк, что у совета при `429` и при гео-блокировке: неверный совет
    хуже отсутствия совета, потому что его выполняют.
    """
    ahead = schema.Mismatch(expected="b8e41c7d3a29", found=UNKNOWN).message

    assert "just migrate" not in ahead
    assert UNKNOWN in ahead


def test_message_when_the_database_was_never_migrated() -> None:
    """Пустая база — своя формулировка: `alembic_version` там нет вовсе."""
    said = schema.Mismatch(expected="b8e41c7d3a29", found=None).message

    assert "just migrate" in said


@pytest.mark.integration
async def test_healthy_schema_passes(migrated_database: str) -> None:
    """На накатанной базе проверка молчит. Иначе она не даёт стартовать вовсе."""
    assert await schema.check() is None
    await schema.require("Воркер не запущен.")


async def _rewind(url: str, revision: str) -> None:
    """Откатить строку `alembic_version`, не трогая саму схему.

    Ровно то состояние, в котором был воркер 7 сентября: таблиц новой миграции
    нет, а код о них знает. Через Alembic это не воспроизвести — `downgrade`
    в проекте запрещён.
    """
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": revision},
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_stale_schema_is_reported_with_both_revisions(migrated_database: str) -> None:
    await _rewind(migrated_database, STALE)

    found = await schema.check()

    assert found is not None
    assert found.found == STALE
    assert found.expected == schema.expected_revision()


@pytest.mark.integration
async def test_worker_does_not_start_on_a_stale_schema(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Путь целиком: `create_app` обязан упасть внятно и до всякой работы.

    Проверяется именно вход воркера, а не `schema.require`: функция может быть
    покрыта и не вызываться нигде — так уже было с `loops.is_stuck`.
    """
    from brain import worker

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    await _rewind(migrated_database, STALE)

    with pytest.raises(SystemExit) as raised:
        async with worker.create_app():
            pass  # pragma: no cover — сюда доходить нельзя

    said = str(raised.value)
    assert "just migrate" in said
    assert STALE in said


@pytest.mark.integration
async def test_bot_does_not_start_on_a_stale_schema(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Бот тоже. Он пишет в те же таблицы и падает так же — просто позже."""
    from brain.adapters.telegram import bot

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    await _rewind(migrated_database, STALE)

    with pytest.raises(SystemExit) as raised:
        await bot.run()

    assert "just migrate" in str(raised.value)


@pytest.mark.integration
async def test_maintenance_does_not_run_on_a_stale_schema(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Команды обслуживания — тот случай, с которого долг и начался 26 августа."""
    from brain import maintenance

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    monkeypatch.setattr("sys.argv", ["maintenance", "reindex", "--dry-run"])
    await _rewind(migrated_database, STALE)

    with pytest.raises(SystemExit) as raised:
        await maintenance.main()

    assert "just migrate" in str(raised.value)
