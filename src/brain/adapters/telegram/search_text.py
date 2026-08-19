"""Как выглядит выдача поиска в Telegram.

Отдельно от обработчика, как и `status_text`: разметка ответа — то, что чаще
всего правится и что проще всего проверить без бота и без базы.

Дата у каждой строки обязательна. Архив нужен, чтобы вспомнить **когда**
что-то было сказано ничуть не реже, чем что именно, — а найденная фраза без
даты выглядит одинаково и вчерашней, и полугодовой.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from brain.core.retrieval.search import Found

NOTHING = "Ничего не нашёл."
EMPTY_QUERY = "После /find напишите, что искать. Например: /find экзамен"

# Сколько текста показывать в строке. Средняя запись архива — 159 символов,
# то есть большинство влезает целиком; обрезка нужна длинным.
PREVIEW = 160


def render(query: str, found: list[Found], *, today: datetime) -> str:
    """Собрать ответ на поиск.

    `today` нужен, чтобы не писать год у сегодняшних записей: «18.08» читается
    быстрее, чем «18.08.2026», а для прошлогодней записи год как раз важен.
    """
    if not found:
        return f"«{query}» — {NOTHING.lower()}"

    lines = [f"Нашёл по запросу «{query}»:", ""]
    lines.extend(_line(item, today=today) for item in found)
    return "\n".join(lines)


def _line(item: Found, *, today: datetime) -> str:
    when = item.created_at.astimezone(ZoneInfo(item.timezone))
    stamp = f"{when:%d.%m}" if when.year == today.year else f"{when:%d.%m.%Y}"
    lock = "🔒 " if item.is_private else ""
    return f"{stamp} · {lock}{_shorten(item.text)}"


def _shorten(text: str) -> str:
    """Обрезать по границе слова: обрубок посреди слова читается как ошибка."""
    text = " ".join(text.split())
    if len(text) <= PREVIEW:
        return text
    return text[:PREVIEW].rsplit(" ", 1)[0] + "…"
