"""Границы единиц: цитата начала превращается в отрезок, или не превращается.

Что должно работать:

1. одна единица — отрезок на всю запись
2. цитата начала находится, и отрезки идут встык, покрывая текст целиком
3. цитата не найдена — нарезка отвергнута целиком, а не подогнана
4. лишние пробелы и переводы строки цитату не ломают
5. регистр цитаты значения не имеет
6. границы, идущие назад, — отказ
7. первая единица начинается с нуля, что бы модель ни процитировала

Пункт 4 не выдуман: расшифровка приходит без нормальной пунктуации, а модель
цитирует, приводя пробелы в порядок. Пункт 3 — единственная защита от того,
чтобы разложить запись по отрезкам, которых в ней нет.
"""

from itertools import pairwise

from brain.core.enrich import units
from brain.storage.models.annotation import AnnotationState, AnnotationType

TEXT = "Первая часть записи. Вторая часть записи. Третья часть записи."


def test_single_unit_covers_everything() -> None:
    located = units.locate(TEXT, [("Первая часть", AnnotationType.THOUGHT)])

    assert located == [units.Unit(start=0, end=len(TEXT), type=AnnotationType.THOUGHT)]


def test_units_are_contiguous_and_cover_the_text() -> None:
    located = units.locate(
        TEXT,
        [
            ("Первая часть", AnnotationType.THOUGHT),
            ("Вторая часть", AnnotationType.TASK),
            ("Третья часть", AnnotationType.LOOP),
        ],
    )

    assert located is not None
    assert [(unit.start, unit.end) for unit in located] == [(0, 21), (21, 42), (42, len(TEXT))]
    # Встык и без дыр: конец каждой — начало следующей.
    assert all(a.end == b.start for a, b in pairwise(located))


def test_missing_quote_rejects_the_whole_segmentation() -> None:
    """Процитировано то, чего в записи нет, — верить нельзя ничему."""
    located = units.locate(
        TEXT,
        [
            ("Первая часть", AnnotationType.THOUGHT),
            ("Четвёртая часть", AnnotationType.TASK),
        ],
    )

    assert located is None


def test_whitespace_does_not_break_the_quote() -> None:
    """Расшифровка переносит строки, модель цитирует через пробел."""
    text = "Первая часть\nзаписи. Вторая\n\nчасть записи."
    located = units.locate(
        text,
        [("Первая часть записи", AnnotationType.THOUGHT), ("Вторая часть", AnnotationType.TASK)],
    )

    assert located is not None
    assert len(located) == 2
    assert text[located[1].start :].startswith("Вторая")


def test_quote_case_does_not_matter() -> None:
    located = units.locate(
        TEXT,
        [("первая ЧАСТЬ", AnnotationType.THOUGHT), ("ВТОРАЯ часть", AnnotationType.TASK)],
    )

    assert located is not None
    assert len(located) == 2


def test_first_unit_always_starts_at_zero() -> None:
    """Начало записи — это начало записи, спорить не о чем."""
    located = units.locate(
        TEXT,
        [("часть записи", AnnotationType.THOUGHT), ("Третья часть", AnnotationType.TASK)],
    )

    assert located is not None
    assert located[0].start == 0


def test_whole_gives_one_unit_even_for_empty_text() -> None:
    """Пустой отрезок в базу не ложится: `span_end > span_start` проверяет она сама."""
    assert units.whole("", AnnotationType.NOISE) == [
        units.Unit(start=0, end=1, type=AnnotationType.NOISE)
    ]


def test_merge_aligns_by_overlap_not_by_position() -> None:
    """Прогоны режут по-разному, и сопоставлять их надо по общему тексту."""
    first = [
        units.Unit(start=0, end=21, type=AnnotationType.THOUGHT),
        units.Unit(start=21, end=62, type=AnnotationType.TASK),
    ]
    # Второй прогон сдвинул границу на четыре символа, но сказал то же самое.
    second = [
        units.Unit(start=0, end=25, type=AnnotationType.THOUGHT),
        units.Unit(start=25, end=62, type=AnnotationType.TASK),
    ]

    merged = units.merge(first, second)

    assert [unit.state for unit in merged] == [AnnotationState.SURE, AnnotationState.SURE]
    assert [(unit.start, unit.end) for unit in merged] == [(0, 21), (21, 62)]


def test_near_tie_overlap_is_a_disagreement_not_a_coin_flip() -> None:
    """Прогоны разошлись ровно посередине единицы — это спор, а не согласие.

    Второй прогон считает первую половину хвоста мыслью, а первый — задачей
    целиком. Перекрытия почти равны, и «победитель» здесь был бы случайным:
    честный ответ — «две гипотезы».
    """
    first = [units.Unit(start=0, end=62, type=AnnotationType.TASK)]
    second = [
        units.Unit(start=0, end=31, type=AnnotationType.THOUGHT),
        units.Unit(start=31, end=62, type=AnnotationType.TASK),
    ]

    merged = units.merge(first, second)

    assert merged[0].state is AnnotationState.TWO_HYPOTHESES


def test_types_of_keeps_order_and_drops_repeats() -> None:
    merged = [
        units.MergedUnit(0, 10, AnnotationType.THOUGHT, None, AnnotationState.SURE),
        units.MergedUnit(10, 20, AnnotationType.TASK, None, AnnotationState.SURE),
        units.MergedUnit(20, 30, AnnotationType.THOUGHT, None, AnnotationState.SURE),
    ]

    assert units.types_of(merged) == [AnnotationType.THOUGHT, AnnotationType.TASK]
