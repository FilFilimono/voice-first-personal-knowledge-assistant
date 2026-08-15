"""Общий базовый класс моделей."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Базовый класс для всех таблиц проекта.

    `metadata` этого класса — то, с чем Alembic сравнивает состояние базы,
    поэтому каждая модель обязана наследоваться отсюда, иначе autogenerate
    её не увидит.
    """
