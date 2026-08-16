"""Файлы голосовых: сохранение и ретеншен.

Аудио живёт на диске, а не в базе: держать в Postgres десятки мегабайт,
которые всё равно удаляются через месяц, незачем.

Через тридцать дней файл удаляется, а строка записи остаётся нетронутой —
`entries` не правится (инвариант 3). Пропавший файл читается как «удалено
по ретеншену», и это дешевле, чем заводить привилегированную роль ради
зачистки одной колонки (цена решения записана в ADR-0003).
"""

import asyncio
from pathlib import Path


async def save(path: str, audio: bytes) -> None:
    """Записать файл, создав каталог. Диск синхронный — уводим в поток."""
    await asyncio.to_thread(_write, Path(path), audio)


def _write(path: Path, audio: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)


async def delete(paths: list[str]) -> int:
    """Удалить файлы, пережившие ретеншен. Вернуть число удалённых.

    Отсутствующий файл — не ошибка: ретеншен мог отработать раньше, а запись
    об этом нигде не ведётся именно потому, что её негде вести.
    """
    return await asyncio.to_thread(_delete_all, paths)


def _delete_all(paths: list[str]) -> int:
    removed = 0
    for path in paths:
        target = Path(path)
        if target.exists():
            target.unlink()
            removed += 1
    return removed
