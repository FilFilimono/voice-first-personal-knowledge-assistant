"""Что должно работать:

1. прогоны совпали — «уверен», альтернативы нет
2. прогоны разошлись — «две гипотезы», и вторая версия названа
3. запись пропущена одним прогоном — «не понял»
4. запись пропущена обоими — «не понял», тип `служебное`
5. тема засчитывается, только если её назвали **оба** прогона
6. «учеба» и «учёба» — одна тема, а не две
7. чужой идентификатор в ответе отбрасывается, а не приписывается соседу

Пункт 5 — не педантизм, а цена, названная в ADR-0011: тема, названная одним
прогоном из двух, теряется. Замер на живом архиве: 33 темы названы обоими,
16 — только одним.

Пункт 7 про то, чего легко не заметить: в пачке идентификаторы идут подряд,
и ошибка на единицу приписала бы разбор соседней записи. Тихо.
"""

from brain.core.enrich.service import Item, _agreed, _merge, _parse
from brain.storage.models.annotation import AnnotationState, AnnotationType


def _item(type_: AnnotationType, topics: list[str] | None = None) -> Item:
    return Item(type=type_, topics=topics or [], entities=[], reason=None)


def test_agreement_gives_sure() -> None:
    merged = _merge([_item(AnnotationType.TASK), _item(AnnotationType.TASK)])

    assert merged.state is AnnotationState.SURE
    assert merged.type is AnnotationType.TASK
    assert merged.alternative_type is None


def test_disagreement_names_both_versions() -> None:
    """«Две гипотезы» без второй версии — состояние без содержания."""
    merged = _merge([_item(AnnotationType.TASK), _item(AnnotationType.EVENT)])

    assert merged.state is AnnotationState.TWO_HYPOTHESES
    assert merged.type is AnnotationType.TASK
    assert merged.alternative_type is AnnotationType.EVENT


def test_skipped_by_one_run_is_unclear() -> None:
    """Пропуск записи прогоном — единственный источник «не понял» в Фазе 3."""
    merged = _merge([_item(AnnotationType.THOUGHT), None])

    assert merged.state is AnnotationState.UNCLEAR
    assert merged.type is AnnotationType.THOUGHT


def test_skipped_by_both_runs_is_noise() -> None:
    """Молчали оба — система не поняла, что это за запись."""
    merged = _merge([None, None])

    assert merged.state is AnnotationState.UNCLEAR
    assert merged.type is AnnotationType.NOISE


def test_topic_named_by_one_run_only_is_dropped() -> None:
    merged = _merge(
        [
            _item(AnnotationType.THOUGHT, ["подготовка к CV", "дисциплина"]),
            _item(AnnotationType.THOUGHT, ["подготовка к CV"]),
        ]
    )

    assert merged.topics == ["подготовка к CV"]


def test_yo_and_ye_are_the_same_topic() -> None:
    """«учеба» и «учёба» вышли из одного прогона модели 19.08.2026."""
    assert _agreed(["учёба"], ["учеба"]) == ["учёба"]


def test_foreign_entry_id_is_dropped() -> None:
    """Ответ про запись, которой в пачке не было, приписывать некому."""
    parsed = _parse(
        {
            "items": [
                {"id": 26, "type": "task", "topics": [], "entities": []},
                {"id": 999, "type": "thought", "topics": [], "entities": []},
            ]
        },
        expected={26},
    )

    assert set(parsed) == {26}


def test_unknown_type_is_dropped_not_guessed() -> None:
    """Значение вне enum'а — повод пропустить запись, а не подобрать похожее."""
    parsed = _parse({"items": [{"id": 1, "type": "выдумка", "topics": [], "entities": []}]}, {1})

    assert parsed == {}
