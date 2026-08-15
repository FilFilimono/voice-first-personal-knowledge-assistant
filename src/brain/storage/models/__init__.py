"""Модели SQLAlchemy.

Пока пусто: схема данных приходит в Фазе 1. Здесь живёт только общий `Base`,
на который смотрит autogenerate в Alembic.
"""

from brain.storage.models.base import Base

__all__ = ["Base"]
