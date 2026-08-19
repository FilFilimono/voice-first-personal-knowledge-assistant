"""Метрики разметки: точность, матрица ошибок и калибровка состояния.

Три числа, и третье важнее первых двух.

**Точность** отвечает на вопрос «часто ли модель права». **Матрица ошибок** —
на вопрос «в какую сторону она ошибается»: девять типов ошибаются
не равномерно, и «задача вместо события» — совсем не то же самое, что
«мысль вместо шума».

**Калибровка** отвечает на вопрос, ради которого состояние вообще заведено:
можно ли верить слову «уверен». Состояние считается по согласию двух прогонов
(ADR-0011), а согласие меряет устойчивость, а не правоту — два прогона могут
согласованно ошибаться. Доля ошибок среди «уверен» — единственное место,
где эта разница видна числом.
"""

from collections import Counter
from dataclasses import dataclass

from brain.storage.models.annotation import AnnotationState, AnnotationType


@dataclass(frozen=True, slots=True)
class Labelled:
    """Одна размеченная владельцем запись."""

    entry_id: int
    expected: AnnotationType
    note: str = ""


@dataclass(frozen=True, slots=True)
class Judged:
    """Что модель сказала про размеченную запись."""

    entry_id: int
    expected: AnnotationType
    got: AnnotationType | None
    alternative: AnnotationType | None
    state: AnnotationState

    @property
    def correct(self) -> bool:
        return self.got is self.expected

    @property
    def correct_within_two(self) -> bool:
        """Засчитывается ли попадание, если верна вторая версия.

        Отдельное число, а не замена точности: «две гипотезы» уезжает
        в вечерний разбор, то есть цену ошибки платит владелец временем,
        а не неверной меткой в архиве.
        """
        return self.correct or self.alternative is self.expected


def accuracy(judged: list[Judged]) -> float:
    if not judged:
        return 0.0
    return sum(item.correct for item in judged) / len(judged)


def accuracy_within_two(judged: list[Judged]) -> float:
    if not judged:
        return 0.0
    return sum(item.correct_within_two for item in judged) / len(judged)


def error_rate_when_sure(judged: list[Judged]) -> float:
    """Доля ошибок среди тех записей, где модель была «уверена».

    Это и есть проверка калибровки. Если она заметно выше нуля, согласие двух
    прогонов не означает правоты — и тогда либо промпт разводит типы плохо,
    либо прогонов нужно больше двух.
    """
    sure = [item for item in judged if item.state is AnnotationState.SURE]
    if not sure:
        return 0.0
    return sum(not item.correct for item in sure) / len(sure)


def confusion(judged: list[Judged]) -> Counter[tuple[str, str]]:
    """Пары «ждали → получили» для всех ошибок."""
    return Counter(
        (item.expected.value, item.got.value if item.got else "—")
        for item in judged
        if not item.correct
    )


def by_state(judged: list[Judged]) -> Counter[str]:
    return Counter(item.state.value for item in judged)
