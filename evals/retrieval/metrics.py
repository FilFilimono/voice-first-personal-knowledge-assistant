"""Метрики ретривала. Отдельно от прогона — их надо уметь читать без базы.

Две штуки, обе из плана:

  recall@k   доля вопросов, у которых хоть одна нужная запись попала в первые k.
             Основная. Отвечает на «нашлось ли вообще»;
  MRR        среднее от 1/позиции первой нужной записи. Отвечает на «не пришлось
             ли за ней лезть вниз списка».

Считаются они по-разному не случайно. Recall@5 не различает первое место
и пятое, MRR различает. Поиск, который всегда ставит нужное пятым, по recall
идеален и невыносим в жизни.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Question:
    """Вопрос набора и записи, которые на него отвечают.

    Записей может быть несколько: «что я думал про поиск» — законный вопрос
    с четырьмя правильными ответами, и требовать от него ровно одного значило
    бы считать три верных попадания ошибками.
    """

    question: str
    relevant: tuple[int, ...]
    note: str = ""


def recall_at(found: Sequence[int], relevant: Sequence[int], k: int) -> float:
    """Попала ли хоть одна нужная запись в первые k. Ноль или единица.

    Именно «хоть одна», а не доля найденных: вопросу к архиву достаточно
    одного верного ответа, чтобы владелец вспомнил остальное сам.
    """
    if not relevant:
        return 0.0
    return 1.0 if set(found[:k]) & set(relevant) else 0.0


def reciprocal_rank(found: Sequence[int], relevant: Sequence[int]) -> float:
    """1 / позиция первой нужной записи. Ноль, если её нет вовсе."""
    for position, entry_id in enumerate(found, start=1):
        if entry_id in relevant:
            return 1.0 / position
    return 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
