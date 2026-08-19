"""Разметка записей дешёвой моделью.

Что здесь лежит:

    prompt.py      что спрашивают у модели и в каком виде она обязана ответить
    service.py     два прогона и правило, по которому из них получается разметка
    repository.py  чтение и запись
    reply.py       что владелец видит, когда разметка готова
    candidates.py  отбор тем, когда весь список уже не помещается в промпт

Наружу торчит немного: воркер зовёт `annotate_pending`, а потом собирает
сообщение из `rows_for_reply` и `render`. Всё остальное — внутреннее.
"""

from brain.core.enrich.reply import NAMES, STORED, Shown, render
from brain.core.enrich.repository import (
    ReplyRow,
    counts_by_state,
    drop_annotations,
    latest_annotation,
    rows_for_reply,
    topics_of,
    unannotated_count,
)
from brain.core.enrich.service import PURPOSE, Outcome, annotate_pending

__all__ = [
    "NAMES",
    "PURPOSE",
    "STORED",
    "Outcome",
    "ReplyRow",
    "Shown",
    "annotate_pending",
    "counts_by_state",
    "drop_annotations",
    "latest_annotation",
    "render",
    "rows_for_reply",
    "topics_of",
    "unannotated_count",
]
