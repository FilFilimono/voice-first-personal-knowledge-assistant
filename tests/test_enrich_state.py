"""Что должно работать:

1. прогоны совпали — «уверен», альтернативы нет
2. прогоны разошлись — «две гипотезы», и вторая версия названа
3. запись пропущена одним прогоном — «не понял»
4. запись пропущена обоими — «не понял», тип `служебное`
5. тема засчитывается, только если её назвали **оба** прогона
6. «учеба» и «учёба» — одна тема, а не две
7. чужой идентификатор в ответе отбрасывается, а не приписывается соседу
8. запись из нескольких единиц получает **все** их типы, а не одну метку
9. состояние считается по единице: бесспорная задача рядом со спорной мыслью
10. не сошлась нарезка — запись целиком «не понял», а не тихо одна единица

Пункт 5 — не педантизм, а цена, названная в ADR-0011: тема, названная одним
прогоном из двух, теряется. Замер на живом архиве: 33 темы названы обоими,
16 — только одним.

Пункт 7 про то, чего легко не заметить: в пачке идентификаторы идут подряд,
и ошибка на единицу приписала бы разбор соседней записи. Тихо.

Пункты 8–10 — ADR-0012. До него запись получала одну метку на всё, и замер
показал цену: из 37 записей архива десять содержат два и более типа, шесть
теряли тип насовсем.
"""

from brain.core.enrich.service import Item, _agreed, _merge, _parse
from brain.core.enrich.units import Unit
from brain.storage.models.annotation import AnnotationState, AnnotationType

# Текст нейтральный: архив в git не уходит (ADR-0002), а этим тестам нужна
# не живая речь, а длины отрезков.
TEXT = "Первая часть записи. Вторая часть записи."
FIRST = (0, 21)
SECOND = (21, len(TEXT))


def _item(
    type_: AnnotationType,
    topics: list[str] | None = None,
    *,
    units: list[Unit] | None = None,
    segmented: bool = True,
) -> Item:
    return Item(
        units=units or [Unit(start=0, end=len(TEXT), type=type_)],
        segmented=segmented,
        topics=topics or [],
        entities=[],
        reason=None,
    )


def _two_units(first: AnnotationType, second: AnnotationType) -> list[Unit]:
    return [
        Unit(start=FIRST[0], end=FIRST[1], type=first),
        Unit(start=SECOND[0], end=SECOND[1], type=second),
    ]


def test_agreement_gives_sure() -> None:
    merged = _merge([_item(AnnotationType.TASK), _item(AnnotationType.TASK)], TEXT)

    assert merged.state is AnnotationState.SURE
    assert merged.type is AnnotationType.TASK
    assert merged.alternative_type is None


def test_disagreement_names_both_versions() -> None:
    """«Две гипотезы» без второй версии — состояние без содержания."""
    merged = _merge([_item(AnnotationType.TASK), _item(AnnotationType.EVENT)], TEXT)

    assert merged.state is AnnotationState.TWO_HYPOTHESES
    assert merged.type is AnnotationType.TASK
    assert merged.alternative_type is AnnotationType.EVENT


def test_skipped_by_one_run_is_unclear() -> None:
    """Пропуск записи прогоном — источник «не понял»."""
    merged = _merge([_item(AnnotationType.THOUGHT), None], TEXT)

    assert merged.state is AnnotationState.UNCLEAR
    assert merged.type is AnnotationType.THOUGHT


def test_skipped_by_both_runs_is_noise() -> None:
    """Молчали оба — система не поняла, что это за запись."""
    merged = _merge([None, None], TEXT)

    assert merged.state is AnnotationState.UNCLEAR
    assert merged.type is AnnotationType.NOISE


def test_topic_named_by_one_run_only_is_dropped() -> None:
    merged = _merge(
        [
            _item(AnnotationType.THOUGHT, ["подготовка к CV", "дисциплина"]),
            _item(AnnotationType.THOUGHT, ["подготовка к CV"]),
        ],
        TEXT,
    )

    assert merged.topics == ["подготовка к CV"]


def test_yo_and_ye_are_the_same_topic() -> None:
    """«учеба» и «учёба» вышли из одного прогона модели 19.08.2026."""
    assert _agreed(["учёба"], ["учеба"]) == ["учёба"]


def test_entry_keeps_every_type_its_units_have() -> None:
    """Главное, ради чего написан ADR-0012.

    До него «подумал вслух, а потом решил» получало одну метку, и вторая
    не доходила до владельца вовсе — на живом архиве так терялись шесть
    записей из 37.
    """
    units = _two_units(AnnotationType.THOUGHT, AnnotationType.TASK)
    merged = _merge(
        [_item(AnnotationType.THOUGHT, units=units), _item(AnnotationType.THOUGHT, units=units)],
        TEXT,
    )

    assert [unit.type for unit in merged.units] == [AnnotationType.THOUGHT, AnnotationType.TASK]
    assert merged.state is AnnotationState.SURE


def test_state_is_per_unit_not_per_entry() -> None:
    """Спорная мысль не делает спорной задачу, стоящую рядом."""
    first = _item(
        AnnotationType.THOUGHT, units=_two_units(AnnotationType.THOUGHT, AnnotationType.TASK)
    )
    second = _item(
        AnnotationType.THOUGHT, units=_two_units(AnnotationType.INSIGHT, AnnotationType.TASK)
    )
    merged = _merge([first, second], TEXT)

    assert [unit.state for unit in merged.units] == [
        AnnotationState.TWO_HYPOTHESES,
        AnnotationState.SURE,
    ]
    assert merged.units[0].alternative_type is AnnotationType.INSIGHT
    # Запись целиком спорна, если спорна хоть одна её единица.
    assert merged.state is AnnotationState.TWO_HYPOTHESES


def test_broken_segmentation_is_unclear_not_silent() -> None:
    """Не сошлась нарезка — это отказ, а не запись без переключений."""
    merged = _merge([_item(AnnotationType.TASK, segmented=False), _item(AnnotationType.TASK)], TEXT)

    assert merged.state is AnnotationState.UNCLEAR
    assert len(merged.units) == 1


def test_leading_type_is_the_longest_unit() -> None:
    """Запись, начинающаяся с «так, ну», — мысль, а не служебное."""
    units = [
        Unit(start=0, end=8, type=AnnotationType.NOISE),
        Unit(start=8, end=len(TEXT), type=AnnotationType.THOUGHT),
    ]
    merged = _merge(
        [_item(AnnotationType.NOISE, units=units), _item(AnnotationType.NOISE, units=units)], TEXT
    )

    assert merged.type is AnnotationType.THOUGHT


def test_foreign_entry_id_is_dropped() -> None:
    """Ответ про запись, которой в пачке не было, приписывать некому."""
    parsed = _parse(
        {
            "items": [
                {
                    "id": 26,
                    "units": [{"start": "Первая", "type": "task"}],
                    "topics": [],
                    "entities": [],
                },
                {
                    "id": 999,
                    "units": [{"start": "Первая", "type": "thought"}],
                    "topics": [],
                    "entities": [],
                },
            ]
        },
        {26: TEXT},
    )

    assert set(parsed) == {26}


def test_unknown_type_is_dropped_not_guessed() -> None:
    """Значение вне enum'а — повод пропустить запись, а не подобрать похожее."""
    parsed = _parse(
        {
            "items": [
                {
                    "id": 1,
                    "units": [{"start": "Первая", "type": "выдумка"}],
                    "topics": [],
                    "entities": [],
                }
            ]
        },
        {1: TEXT},
    )

    assert parsed == {}
