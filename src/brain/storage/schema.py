"""Совпадает ли схема базы с той, которую ждёт код.

**Заведено 7 сентября 2026, после второго падения одного и того же почерка.**
26 августа непринятая миграция уронила `just backfill-titles`
(`invalid input value for enum commitment_state: "in_progress"`), 7 сентября —
воркер (`relation "habits" does not exist`), и уронила его **на каждом тике,
каждые пять минут**. Оба раза код знал новые таблицы и состояния, а база нет.

Проверка вызовом при старте была у моделей и не была у схемы, хотя цена ошибки
та же: **система выглядит рабочей и падает на первом настоящем действии.**
Разница только в том, что модель отвечает по сети и её приходится спрашивать,
а версия схемы лежит в самой базе одной строкой — то есть проверка стоит один
запрос и снимает целый класс отказов.

Отсюда устройство модуля — три вещи и ничего больше:

    голова из истории    что накатит `just migrate`. Берётся у Alembic,
                         а не переписывается константой: своя копия номера
                         разошлась бы с историей молча, и старт валился бы
                         на здоровой базе;
    строка из базы       что накачено на самом деле;
    совет по расхождению **разный**, а не один на все случаи. База отстала —
                         `just migrate`; база впереди кода — миграции тут
                         не при чём, и обещать их нельзя. Тот же вывод, что
                         у совета при `429` и при гео-блокировке: неверный
                         совет хуже отсутствия совета, потому что его выполняют.

Зовут это воркер, бот и команды обслуживания — то есть все три входа, каждый
из которых до сих пор падал трассировкой.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from brain.storage.db import get_engine

logger = logging.getLogger(__name__)

# Каталог миграций берётся по своему же расположению, а не через `alembic.ini`.
# Файл конфигурации нужен, чтобы **накатывать** миграции; чтобы узнать голову,
# хватает самих файлов, и лишняя зависимость от корня репозитория здесь была бы
# ровно тем, что ломается при запуске из другого каталога.
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def script() -> ScriptDirectory:
    """История миграций проекта."""
    return ScriptDirectory(str(MIGRATIONS))


def expected_revision() -> str:
    """Ревизия, которую ждёт код. Она же — то, чем кончится `just migrate`.

    Голова обязана быть одна: у истории с двумя головами `upgrade head` падает,
    и это проверяется отдельным быстрым тестом (`test_migrations`).
    """
    head = script().get_current_head()
    if head is None:  # pragma: no cover — история пуста только до первой миграции
        raise RuntimeError("в истории миграций нет ни одной ревизии")
    return head


async def current_revision() -> str | None:
    """Ревизия, накатанная на базу. `None` — база не размечена Alembic вовсе.

    Отсутствие таблицы `alembic_version` — не ошибка, а состояние: так выглядит
    свежесозданная база. Ловится оно исключением, потому что другого способа
    спросить у Postgres «есть ли таблица» без второго запроса нет.
    """
    try:
        async with get_engine().connect() as connection:
            found = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError:
        return None
    return None if found is None else str(found)


@dataclass(frozen=True, slots=True)
class Mismatch:
    """Расхождение схемы с кодом — и что с ним делать."""

    expected: str
    # `None` — базу не мигрировали ни разу.
    found: str | None

    @property
    def stale(self) -> bool:
        """Отстала ли база. `False` означает, что она ушла вперёд кода.

        Различается по истории: ревизия, которую Alembic знает, — это прошлое
        проекта, а незнакомая — его будущее, то есть ветка кода, которой здесь
        нет. Советы у этих случаев противоположные.
        """
        if self.found is None:
            return True
        try:
            script().get_revision(self.found)
        except Exception:
            return False
        return True

    @property
    def message(self) -> str:
        """Одна внятная строка вместо ста двадцати строк трассировки."""
        if self.found is None:
            return (
                "База не размечена миграциями: таблицы alembic_version в ней нет.\n"
                f"Код ждёт схему {self.expected}.\n\n"
                "Накатите миграции: just migrate"
            )
        if self.stale:
            return (
                f"Схема базы отстала от кода: накачено {self.found}, "
                f"код ждёт {self.expected}.\n\n"
                "Накатите миграции: just migrate"
            )
        # `just migrate` здесь не поможет: голова кода **старше** того, что
        # в базе, и `upgrade head` не сделает ничего. Лечится это кодом,
        # а не схемой, и говорить надо именно это.
        return (
            f"Схема базы новее кода: в базе {self.found}, "
            f"а история кода кончается на {self.expected}.\n\n"
            "Это не миграции — это код отстал. Обновите ветку."
        )


async def check() -> Mismatch | None:
    """Сверить схему с кодом. `None` — всё сошлось."""
    expected = expected_revision()
    found = await current_revision()
    if found == expected:
        return None
    return Mismatch(expected=expected, found=found)


async def require(what: str) -> None:
    """Не дать процессу подняться на чужой схеме.

    `what` — первая строка сообщения: «Воркер не запущен.», «Бот не запущен.».
    Приходит от вызывающего, потому что читать её будет владелец, а он запускал
    не «процесс», а конкретную команду.

    Наружу уходит `SystemExit`, а не исключение, — по той же причине, по которой
    так устроен `get_runtime_settings`: над трассировкой на сто двадцать кадров
    единственная нужная строка теряется.
    """
    found = await check()
    if found is None:
        logger.info("схема базы: %s", expected_revision())
        return
    raise SystemExit(f"\n{what}\n{found.message}\n")
