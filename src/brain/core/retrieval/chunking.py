"""Резка записи на куски, которые пойдут в поиск.

Чанк — это то, что получает свой эмбеддинг и свою строку в индексе. Длина
чанка решает, что именно найдётся: короткий теряет контекст, длинный
размывает смысл в одном усреднённом векторе.

**Почему по предложениям, а не по абзацам.** ADR-0006 говорит «по абзацам
и паузам», и для набранного текста это верно. Но 22 записи архива из 31 —
голос, а у расшифровки переводов строки нет вовсе: в записи 31 (196 секунд,
1958 символов) их ноль. Абзацев в устной речи не существует, зато Gemini
расставляет точки — 26 предложений на ту же запись, медиана 51 символ.
Предложение и есть единственная граница, которая в этих данных встречается.

Паузы из расшифровки в качестве границы рассматривались и отклонены: за ними
пришлось бы просить у модели таймкоды, то есть править промпт расшифровки —
а он написан так, чтобы модель отдавала речь дословно и ничего больше.
Менять его ради резки значит рисковать дословностью и корпусом eval'а STT.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Порог из ADR-0006: короче — чанк равен всей записи. Под него попадают
# 30 записей архива из 31, и это не совпадение, а обычная длина мысли.
MAX_SINGLE_CHUNK = 1500

# Окно для длинной записи. Меньше порога намеренно: смысл резки в том, чтобы
# у длинной записи было несколько разных векторов, а не два почти одинаковых.
WINDOW = 900

# Доля окна, на которую соседние чанки перекрываются. Нужна ровно для одного:
# мысль, попавшая на стык, иначе не находится ни по одному из двух чанков.
OVERLAP_RATIO = 0.15
OVERLAP = int(WINDOW * OVERLAP_RATIO)

# Конец предложения: знак и пробел после него. Пробел обязателен — без него
# «16.08» и «18.08» из записи 31 разошлись бы на четыре обрывка.
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split(text: str) -> list[str]:
    """Разрезать текст записи. Короткий возвращается одним куском."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_SINGLE_CHUNK:
        return [text]

    return _pack(_fit_to_window(sentences(text)))


def split_entry(entry_id: int, text: str) -> list[str]:
    """То же самое, но со следом в логе, когда правило резки сработало.

    Отдельная функция, потому что `split` ничего не знает про запись, а в логе
    без её номера смысла нет. Пишется только срабатывание: правило редкое —
    за всю историю архива оно случилось один раз, — и правило, которое молчит,
    невозможно проверить, пока оно молча не сработает неправильно.
    """
    chunks = split(text)
    if len(chunks) > 1:
        logger.info(
            "запись разрезана: entry_id=%s, длина=%s, чанков=%s, длины=%s",
            entry_id,
            len(text.strip()),
            len(chunks),
            [len(chunk) for chunk in chunks],
        )
    return chunks


def sentences(text: str) -> list[str]:
    """Предложения текста. Знак препинания остаётся при своём предложении."""
    return [part for part in SENTENCE_END.split(text.strip()) if part]


def _fit_to_window(parts: list[str]) -> list[str]:
    """Развалить куски, которые сами по себе длиннее окна.

    В архиве таких нет, но расшифровка уже роняла окончания и съедала слоги —
    точку она может не поставить так же легко, и тогда вся запись придёт одним
    «предложением». Режется по границам слов: разрезанное посередине слово
    не найдётся ни полнотекстом, ни триграммами.
    """
    fitted: list[str] = []
    for part in parts:
        if len(part) <= WINDOW:
            fitted.append(part)
            continue
        fitted.extend(_split_by_words(part))
    return fitted


def _split_by_words(part: str) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    length = 0

    for word in part.split():
        addition = len(word) + (1 if current else 0)
        if current and length + addition > WINDOW:
            pieces.append(" ".join(current))
            current, length = [], 0
            addition = len(word)
        current.append(word)
        length += addition

    if current:
        pieces.append(" ".join(current))
    return pieces


def _pack(parts: list[str]) -> list[str]:
    """Набить предложения в окна с перекрытием.

    Перекрытие берётся целыми предложениями с конца предыдущего чанка: резать
    его по символам значило бы вернуть ровно ту проблему, ради которой резка
    идёт по предложениям.
    """
    chunks: list[str] = []
    current: list[str] = []

    for part in parts:
        if current and _length(current) + 1 + len(part) > WINDOW:
            chunks.append(" ".join(current))
            current = _tail(current, len(part))
        current.append(part)

    if current:
        chunks.append(" ".join(current))
    return chunks


def _tail(parts: list[str], next_length: int) -> list[str]:
    """Хвост предыдущего чанка, который уедет в начало следующего.

    Набирается с конца, пока не наберётся `OVERLAP` символов, и урезается так,
    чтобы следующее предложение точно поместилось: иначе перекрытие съело бы
    окно целиком и резка встала бы на месте.
    """
    tail: list[str] = []
    for part in reversed(parts):
        if _length(tail) >= OVERLAP:
            break
        tail.insert(0, part)

    while tail and _length(tail) + 1 + next_length > WINDOW:
        tail.pop(0)
    return tail


def _length(parts: list[str]) -> int:
    """Длина склейки кусков через пробел."""
    if not parts:
        return 0
    return sum(len(part) for part in parts) + len(parts) - 1
