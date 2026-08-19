"""Типы колонок, которых нет в SQLAlchemy.

Пока здесь один — `vector` из расширения pgvector.

**Почему не пакет `pgvector`.** Он есть и он хороший, но новая зависимость
в этом проекте стоит ADR, а весь нужный протокол — это перевод списка чисел
в строку `[1,2,3]` и обратно. Проверено вызовом на живой базе: asyncpg
принимает такую строку в колонку `vector` без всякого кодека и возвращает
её же при чтении. Двадцать строк здесь дешевле, чем зависимость навсегда.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import Dialect
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """Колонка `vector(N)` из pgvector.

    Размерность попадает в схему и меняется болезненно — перегенерация архива,
    миграция колонки, пересборка индекса (ADR-0006). Поэтому она приходит
    сюда числом, а не настройкой: настройку можно поменять в `.env` и узнать
    об этом от базы в проде.
    """

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        super().__init__()
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect: Dialect) -> Callable[[list[float] | None], Any]:
        def process(value: list[float] | None) -> str | None:
            return None if value is None else to_literal(value)

        return process

    def result_processor(
        self, dialect: Dialect, coltype: Any
    ) -> Callable[[Any], list[float] | None]:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else from_literal(value)

        return process


def to_literal(vector: list[float]) -> str:
    """Вектор в том виде, в каком его понимает pgvector: `[1,2,3]`."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def from_literal(literal: str) -> list[float]:
    """Обратно. Базы отдают вектор именно строкой."""
    return [float(part) for part in literal.strip("[]").split(",") if part]
