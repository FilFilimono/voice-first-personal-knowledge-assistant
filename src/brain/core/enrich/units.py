"""Границы смысловых единиц: где они на самом деле и что делать, когда не сходятся.

Модель возвращает не отрезки, а **цитаты начала** каждой единицы: считать
символы модели не умеют, а цитату можно найти в тексте и проверить. Здесь
цитаты превращаются в отрезки, проверяется покрытие и сводятся два прогона.

**Почему проверка обязательна.** Границы предлагает модель (ADR-0012), то есть
это ещё одно место, где чужой ответ принимается на веру. Ненайденная цитата
означает, что модель процитировала не то, что записано, — и молча принять
такое значило бы разложить запись по отрезкам, которых в ней нет. Отказ здесь
деградирует в прежнее поведение: одна единица на всю запись.

**Почему поиск цитаты нестрогий.** Расшифровка приходит без нормальной
пунктуации и с переносами строк, а модель цитирует, приводя пробелы в порядок.
Сравнение слово-в-слово по любым пробелам — единственный способ не терять
границу из-за лишнего перевода строки.
"""

import logging
import re
from dataclasses import dataclass

from brain.storage.models.annotation import AnnotationState, AnnotationType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Unit:
    """Одно дело внутри записи. Отрезок полуоткрытый: `[start, end)`.

    Дело, а не отрезок с одним типом: две задачи подряд — две единицы,
    и схлопывать их нельзя, иначе Фаза 4 не сможет сказать, сколько дел
    осталось висеть (ADR-0012, блок «Обновление»).
    """

    start: int
    end: int
    type: AnnotationType

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class MergedUnit:
    """Единица после сведения двух прогонов."""

    start: int
    end: int
    type: AnnotationType
    alternative_type: AnnotationType | None
    state: AnnotationState


def locate(text: str, starts: list[tuple[str, AnnotationType]]) -> list[Unit] | None:
    """Превратить цитаты начал в отрезки. `None`, если нарезка не сошлась.

    Первая единица всегда начинается с нуля, что бы модель ни процитировала:
    начало записи — это начало записи, и спорить тут не о чем. Остальные
    ищутся по порядку, каждая после предыдущей.
    """
    if not starts:
        return None

    positions = [0]
    cursor = 0
    for quote, _ in starts[1:]:
        found = _find(text, quote, cursor + 1)
        if found is None:
            logger.info("нарезка не сошлась: цитата %r не найдена в записи", quote[:60])
            return None
        positions.append(found)
        cursor = found

    if positions != sorted(set(positions)):
        logger.info("нарезка не сошлась: границы идут не по порядку — %s", positions)
        return None

    bounds = [*positions[1:], len(text)]
    return [
        Unit(start=start, end=end, type=type_)
        for start, end, (_, type_) in zip(positions, bounds, starts, strict=True)
        if end > start
    ]


def whole(text: str, type_: AnnotationType) -> list[Unit]:
    """Одна единица на всю запись — то, чем всё было до ADR-0012.

    Сюда приходят и записи без переключений (их большинство), и записи,
    у которых нарезка не сошлась. Различает эти два случая состояние,
    а не форма: у второго оно «не понял».
    """
    return [Unit(start=0, end=max(len(text), 1), type=type_)]


def merge(first: list[Unit], second: list[Unit]) -> list[MergedUnit]:
    """Свести два прогона. Нарезка берётся у первого.

    Прогоны могут разрезать запись по-разному, и «правильной» из двух нарезок
    нет: выбирать по числу единиц значило бы поощрять дробление, а по длине —
    поощрять слипание. Берётся первая, ровно по тому же правилу, по которому
    написание темы берётся у первого прогона (ADR-0011).

    Тип сравнивается по **перекрытию**: единице первого прогона отвечает та
    единица второго, с которой она делит больше всего текста. Это и есть
    «прогоны сказали про одно место разное» в честном виде.
    """
    merged: list[MergedUnit] = []
    for unit in first:
        counterpart = _overlapping(unit, second)
        agreed = counterpart is not None and counterpart.type is unit.type
        merged.append(
            MergedUnit(
                start=unit.start,
                end=unit.end,
                type=unit.type,
                alternative_type=None if agreed or counterpart is None else counterpart.type,
                state=AnnotationState.SURE if agreed else AnnotationState.TWO_HYPOTHESES,
            )
        )
    return merged


def summary(units: list[MergedUnit]) -> tuple[AnnotationType, AnnotationType | None]:
    """Главный тип записи и его альтернатива — по самой длинной единице.

    «Главный» решается длиной, а не порядком: запись, которая начинается
    с «так, ну» и дальше на четыреста символов рассуждает, — это мысль,
    а не служебное.
    """
    leading = max(units, key=lambda unit: (unit.end - unit.start, -unit.start))
    return leading.type, leading.alternative_type


def types_of(units: list[MergedUnit]) -> list[AnnotationType]:
    """Сводный набор типов записи, в порядке появления и без повторов."""
    seen: list[AnnotationType] = []
    for unit in units:
        if unit.type not in seen:
            seen.append(unit.type)
    return seen


def _find(text: str, quote: str, since: int) -> int | None:
    """Позиция цитаты в тексте, начиная с `since`. Пробелы не в счёт."""
    words = quote.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    match = re.compile(pattern, re.IGNORECASE).search(text, since)
    return None if match is None else match.start()


def _overlapping(unit: Unit, others: list[Unit]) -> Unit | None:
    """Единица другого прогона, делящая с этой больше всего текста."""
    best: Unit | None = None
    best_overlap = 0
    for other in others:
        overlap = min(unit.end, other.end) - max(unit.start, other.start)
        if overlap > best_overlap:
            best, best_overlap = other, overlap
    return best
