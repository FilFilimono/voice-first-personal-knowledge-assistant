"""Разметка пачки записей: два прогона и то, что из них следует.

Правило целиком записано в ADR-0011 (`docs/adr/0011-annotation-types-state-and-topics.md`),
здесь его реализация. Коротко:

    прогоны совпали                         → sure
    прогоны разошлись                       → two_hypotheses, обе версии
    запись пропущена хотя бы одним прогоном → unclear

**Почему два прогона, а не самооценка модели.** Замер 19.08.2026 на живом
архиве: одиночный прогон вернул состояние «уверен» на 29 записях из 29 — обе
модели, включая запись «в 14:50» длиной в семь символов. Enum самооценки
из ADR-0003 в честном виде не работает: ветки «две гипотезы» и «не понял»
не срабатывали ни разу. Согласие двух прогонов, в отличие от самоотчёта, —
величина наблюдаемая: при температуре 1.0 расхождение вышло на 5 записях
из 29, и ровно на тех, где задумается и человек.

**Почему разметка — задание в очереди, а не работа при приёме.** Та же причина,
что у расшифровки и индексации: это сетевой вызов, он падает, он ждёт, он стоит
денег, а подтверждение приёма должно уйти быстрее 300 мс. Задание ставится
**в транзакции приёма** (инвариант 7): запись без задания — это запись, которую
никогда не разберут, и узнать об этом было бы неоткуда.

**Про пачку.** Обработчик забирает всё неразмеченное разом, а не одну запись:
при простое это пачка из одной штуки и ответ через секунды, при всплеске —
настоящая пачка. Названо вслух: на наших объёмах батчинг экономит центы,
его польза в другом — вся пачка видит один и тот же список тем.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from brain.core import llm
from brain.core.enrich import candidates, prompt, repository, units
from brain.core.ports import Embedder, EntryText, LanguageModel
from brain.core.retrieval.embeddings import embed_topic_names
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.topic import normalize

logger = logging.getLogger(__name__)

PURPOSE = "enrich"

# Температура ненулевая намеренно, и это ровно обратное тому, что стоит
# в расшифровке. Там разброс только вредит; здесь он измерительный прибор —
# без него два прогона совпадут всегда, и состояние снова станет самоотчётом.
# Заодно замер показал, что 0.0 у этого провайдера ничего не фиксирует:
# три прогона при нуле разошлись на 17 записях из 29 (ADR-0011).
TEMPERATURE = 1.0

RUNS = 2

# Пока тем меньше — модель видит весь список. Замер: при списке из 32 тем
# top-10 по эмбеддингу накрывает 84% нужных тем, а полный список стоит
# 130 токенов. Экономить сто токенов ценой каждой шестой темы незачем.
TOPIC_LIST_LIMIT = 150


@dataclass(frozen=True, slots=True)
class Item:
    """Что один прогон сказал про одну запись."""

    units: list[units.Unit]
    # Сошлась ли нарезка. Не сошлась — единица одна на всю запись, и запись
    # уедет в «не понял»: это отказ, а не запись без переключений (ADR-0012).
    segmented: bool
    topics: list[str]
    entities: list[str]
    reason: str | None


@dataclass(frozen=True, slots=True)
class Merged:
    """Что из двух прогонов получилось."""

    units: list[units.MergedUnit]
    type: AnnotationType
    alternative_type: AnnotationType | None
    state: AnnotationState
    topics: list[str]
    entities: list[str]
    reason: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """Итог прохода — то, что уходит в лог и в ответ владельцу."""

    annotated: int = 0
    disagreed: int = 0
    unclear: int = 0
    new_topics: list[str] = field(default_factory=list)
    # Кого именно разметили. Нужно воркеру: он дописывает разметку
    # к подтверждению приёма, а какие записи попали в пачку, знает только
    # тот, кто её собирал.
    entry_ids: list[int] = field(default_factory=list)


async def annotate_pending(
    model: LanguageModel,
    embedder: Embedder,
    *,
    limit: int,
) -> Outcome:
    """Разметить всё, что ждёт разметки. Вернуть итог прохода."""
    entries = await repository.entries_needing_annotation(limit)
    if not entries:
        return Outcome()

    known = await repository.known_topics()
    if len(known) > TOPIC_LIST_LIMIT:
        # Список перерос промпт — показываем не все темы, а близкие к пачке.
        # Момент этот наступит нескоро, но написан он сейчас: в тот день
        # выяснять, почему разметка вдруг стоит вдвое, будет некогда.
        known = await _candidates(known, entries)

    system = prompt.build_system([topic.name for topic in known])
    by_slug = {topic.slug: topic for topic in known}

    texts = {entry.entry_id: entry.text for entry in entries}
    runs = [await _run(model, entries, system) for _ in range(RUNS)]

    merged = {
        entry.entry_id: _merge([run.get(entry.entry_id) for run in runs], texts[entry.entry_id])
        for entry in entries
    }

    # Вектора новых тем считаются один раз на пачку: одну и ту же тему могут
    # предложить две записи подряд, и платить за неё дважды незачем.
    fresh = {
        normalize(name): name
        for result in merged.values()
        for name in result.topics
        if normalize(name) not in by_slug
    }
    vectors = await _topic_vectors(embedder, list(fresh.values()))

    # Над каким текстом посчитаны отрезки. Спрашивается один раз на пачку.
    transcripts = await repository.transcript_ids([entry.entry_id for entry in entries])

    saved: list[int] = []
    for entry_id, result in merged.items():
        existing = [
            by_slug[normalize(name)].id for name in result.topics if normalize(name) in by_slug
        ]
        new_topics = [
            repository.NewTopic(
                name=name,
                reason=result.reason,
                embedding=vectors.get(name),
                embedding_model=embedder.model if vectors else None,
            )
            for name in result.topics
            if normalize(name) not in by_slug
        ]

        await repository.save_annotation(
            entry_id=entry_id,
            type_=result.type,
            alternative_type=result.alternative_type,
            state=result.state,
            entities=result.entities,
            provider=model.provider,
            model=model.model,
            existing_topic_ids=existing,
            new_topics=new_topics,
            units=result.units,
            transcript_id=transcripts.get(entry_id),
            # Текст нужен делам: они хранят снимок своего куска, а не только
            # отрезок, — иначе сводка перестанет их показывать после перепрогона
            # расшифровки, сдвинувшего границы.
            text=texts[entry_id],
        )
        saved.append(entry_id)

    states = [result.state for result in merged.values()]
    outcome = Outcome(
        annotated=len(saved),
        disagreed=states.count(AnnotationState.TWO_HYPOTHESES),
        unclear=states.count(AnnotationState.UNCLEAR),
        new_topics=sorted(fresh.values()),
        entry_ids=saved,
    )

    logger.info(
        "разметка: записей %s, расхождений %s, непонятых %s, новых тем %s",
        outcome.annotated,
        outcome.disagreed,
        outcome.unclear,
        len(outcome.new_topics),
    )
    return outcome


async def _run(
    model: LanguageModel, entries: list[EntryText], system: str
) -> dict[int, Item | None]:
    """Один прогон по пачке. Ключ — идентификатор записи."""
    completion = await llm.complete_over_entries(
        model,
        entries=entries,
        system=system,
        schema=prompt.response_schema(),
        temperature=TEMPERATURE,
        purpose=PURPOSE,
    )
    return _parse(completion.data, {entry.entry_id: entry.text for entry in entries})


def _parse(data: dict[str, Any], texts: dict[int, str]) -> dict[int, Item | None]:
    """Разобрать ответ прогона, не принимая чужие идентификаторы за свои.

    Модель может вернуть запись, которой в пачке не было, — и это не курьёз:
    в пачке идентификаторы идут подряд, и ошибиться на единицу легко. Молча
    приняв такой ответ, мы приписали бы разметку чужой записи.

    Здесь же цитаты начал превращаются в отрезки. Не сошлись — запись получает
    одну единицу на всё и поедет в «не понял»: см. `units.locate`.
    """
    items: dict[int, Item | None] = {}
    for raw in data.get("items", []):
        try:
            entry_id = int(raw["id"])
            starts = [
                (str(unit["start"]), AnnotationType(unit["type"])) for unit in raw["units"] or []
            ]
        except (KeyError, TypeError, ValueError):
            logger.warning("разметка не разобрана: %s", str(raw)[:200])
            continue

        if entry_id not in texts:
            logger.warning("модель вернула запись %s, которой в пачке не было", entry_id)
            continue
        if not starts:
            logger.warning("модель не вернула ни одной единицы для записи %s", entry_id)
            continue

        text = texts[entry_id]
        located = units.locate(text, starts)
        if located is None:
            logger.info("запись %s: нарезка не сошлась, беру запись целиком", entry_id)

        items[entry_id] = Item(
            units=located if located is not None else units.whole(text, starts[0][1]),
            segmented=located is not None,
            topics=[str(name) for name in raw.get("topics", []) if str(name).strip()],
            entities=[str(name) for name in raw.get("entities", []) if str(name).strip()],
            reason=(str(raw["new_topic_reason"]) if raw.get("new_topic_reason") else None),
        )
    return items


def _merge(items: list[Item | None], text: str) -> Merged:
    """Свести прогоны в одну разметку. Правило целиком — в ADR-0011 и ADR-0012.

    Состояние считается **по единице**, а состояние записи собирается из них:
    одна запись может содержать бесспорную задачу и спорную мысль, и называть
    её целиком спорной было бы неправдой в обе стороны.
    """
    first, second = items[0], items[1]

    if first is None or second is None:
        # Пропуск записи прогоном — источник «не понял», как и было в ADR-0011.
        # Разметка берётся у того прогона, который ответил; если молчали оба,
        # честнее всего `noise`: система не поняла, что это за запись.
        answered = first or second
        logger.info("прогон пропустил запись: состояние «не понял»")
        fallback = answered.units if answered else units.whole(text or " ", AnnotationType.NOISE)
        unclear = [
            units.MergedUnit(
                start=unit.start,
                end=unit.end,
                type=unit.type,
                alternative_type=None,
                state=AnnotationState.UNCLEAR,
            )
            for unit in fallback
        ]
        type_, alternative = units.summary(unclear)
        return Merged(
            units=unclear,
            type=type_,
            alternative_type=alternative,
            state=AnnotationState.UNCLEAR,
            topics=[],
            entities=answered.entities if answered else [],
            reason=None,
        )

    merged_units = units.merge(first.units, second.units)
    type_, alternative = units.summary(merged_units)

    # Не сошлась нарезка хотя бы у одного прогона — запись целиком «не понял».
    # Типы при этом сохраняются: они могут быть верны, а вот утверждать, что
    # разбор полный, оснований нет.
    if not (first.segmented and second.segmented):
        state = AnnotationState.UNCLEAR
    elif any(unit.state is AnnotationState.TWO_HYPOTHESES for unit in merged_units):
        state = AnnotationState.TWO_HYPOTHESES
    else:
        state = AnnotationState.SURE

    return Merged(
        units=merged_units,
        type=type_,
        alternative_type=alternative,
        state=state,
        topics=_agreed(first.topics, second.topics),
        entities=_agreed(first.entities, second.entities),
        reason=first.reason or second.reason,
    )


def _agreed(first: list[str], second: list[str]) -> list[str]:
    """То, что назвали оба прогона. Сравнение по нормализованному имени.

    Написание берётся у первого прогона: «учеба» и «учёба» — одна тема, но
    показать надо какое-то одно из двух, и выбирать по алфавиту было бы
    произволом ничем не лучше.

    Названное одним прогоном отбрасывается. Цена названа в ADR-0011: треть
    первых тем появится не с первой записи, а со второй-третьей на ту же тему.
    """
    other = {normalize(name) for name in second}
    seen: set[str] = set()
    result = []
    for name in first:
        slug = normalize(name)
        if slug in other and slug not in seen:
            seen.add(slug)
            result.append(name)
    return result


async def _topic_vectors(embedder: Embedder, names: list[str]) -> dict[str, list[float]]:
    """Вектора названий новых тем. Пустой ответ, если считать нечем.

    Отказ провайдера здесь не должен ронять разметку: вектор названия нужен
    отбору кандидатов, а он включается на полутора сотнях тем. Разметка без
    вектора — работающая разметка, разметка, упавшая из-за вектора, — потеря.
    """
    if not names:
        return {}
    try:
        vectors = await embed_topic_names(embedder, names)
    except Exception:
        logger.exception("вектора новых тем не посчитались — темы заведутся без них")
        return {}
    return dict(zip(names, vectors, strict=True))


async def _candidates(
    known: list[repository.KnownTopic], entries: list[EntryText]
) -> list[repository.KnownTopic]:
    """Темы, близкие к пачке, когда весь список уже не показать.

    Отбор идёт по вектору записи против векторов названий — замер 19.08.2026
    даёт 84% попадания при десяти кандидатах на запись (ADR-0011).
    """
    return await candidates.nearest(known, [entry.entry_id for entry in entries])
