"""Переобобщение тем: то, что разметка по одной записи сделать не может.

**Зачем это вообще есть.** Замер 19.08.2026 в прод-режиме — по одной записи,
как работает воркер, — дал две крайности и ни одной середины:

    список тем пуст      → россыпь: 18 тем на 14 записей, почти все по разу
    список тем растёт    → колодец: одна тема на 11 записей подряд

Причина не в промпте. Обобщать модель умеет, только когда видит пачку:
двадцать записей разом позволяют заметить, что четыре из них про одно, и назвать
это одним словом. На одной записи обобщать не из чего — и дальше всё решает
инструкция про существующие темы, в какую бы сторону её ни повернуть.

Поэтому способность обобщать возвращается отдельным проходом: раз в N записей
накопленное уходит **одной пачкой**, и модель раскладывает его по темам,
видя всё сразу. Это не украшение списка, а единственное место, где темы
вообще получаются (`vision.md` §5.10).

**Слияние дублей происходит само.** Отдельного протокола «слей A и B» нет:
проход переписывает привязки тем целиком, и тема, которой после этого никто
не пользуется, удаляется. Просить у модели список слияний значило бы завести
второй способ сказать то же самое — а он однажды разойдётся с первым.

**Базовые темы не удаляются никогда.** Они заведены миграцией, а не записью,
и пустая базовая тема — это не мусор, а список, которому пока не из чего
наполниться.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from brain.core import llm
from brain.core.enrich import repository
from brain.core.ports import Embedder, EntryText, LanguageModel
from brain.core.retrieval.embeddings import embed_topic_names
from brain.storage.models.topic import normalize

logger = logging.getLogger(__name__)

PURPOSE = "regeneralize"

# Столько записей должно накопиться, чтобы проход имел смысл. Меньше десятка —
# и обобщать снова не из чего: ровно та беда, ради которой проход написан.
EVERY = 10

# Потолок пачки. Он же потолок осмысленности: пятьдесят записей в одном запросе
# модель разбирает хуже, чем двадцать, — а обобщение и есть то, что портится
# первым.
BATCH = 40

# Температура ниже, чем у разметки. Там разброс — измерительный прибор
# (состояние из расхождения двух прогонов, ADR-0011), здесь он только вредит:
# проход переписывает темы всего архива, и «каждый раз по-новому» означало бы,
# что список тем не сходится никогда.
TEMPERATURE = 0.3

RUNS = 2


@dataclass(frozen=True, slots=True)
class Regeneralized:
    """Итог прохода — то, что уходит в лог и владельцу."""

    entries: int = 0
    topics_before: int = 0
    topics_after: int = 0
    merged_away: list[str] = field(default_factory=list)


SYSTEM = """Ты приводишь в порядок темы личного архива мыслей одного человека.

Ниже — записи архива целиком и список тем, которые заведены сейчас. Список
собирался по одной записи за раз, поэтому в нём бывает двоякий мусор: темы,
под которые попало всё подряд, и темы, использованные ровно один раз.

Твоя задача — разложить записи по темам заново, видя их все сразу.

Правила:
- Тема — о чём запись по существу: предмет, проект, человек, область, страна.
  То, что система с записью делает, темой не бывает: «напоминание»,
  «планирование», «заметка» — не темы.
- **Хорошая тема объединяет несколько записей.** Тема на одну запись из сорока
  почти всегда лишняя: либо она часть темы покрупнее, либо темы здесь нет.
- **У записи может не быть темы**, и это нормальный ответ. У «напомни уйти
  домой в 19:40» темы нет.
- Имена существующих тем переписывай ровно так, как они даны, если тема
  подходит. Если несколько существующих тем — про одно и то же, выбери одно
  имя и поставь его всем таким записям: лишняя тема исчезнет сама.
- Заводи новую тему, когда несколько записей про одно, а имени для этого
  в списке нет.

Ответь про каждую запись ровно один раз, с тем же id."""


def response_schema() -> dict[str, Any]:
    """Схема ответа прохода. Единиц и типов здесь нет — это работа разметки."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "topics": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 3,
                        },
                    },
                    "required": ["id", "topics"],
                },
            }
        },
        "required": ["items"],
    }


async def due() -> int:
    """Сколько записей накопилось с прошлого прохода.

    Считается **по данным**, а не по журналу очереди: временем прошлого прохода
    служит последний удавшийся вызов модели с этим назначением. Так же устроены
    `just reindex` и `just reenrich`, и по той же причине — задание может
    исчезнуть, а данные остаются.
    """
    return await repository.entries_since_last(PURPOSE)


async def regeneralize(model: LanguageModel, embedder: Embedder) -> Regeneralized:
    """Переразложить архив по темам. Один проход, вся пачка сразу."""
    entries = await repository.recent_entries(BATCH)
    if not entries:
        return Regeneralized()

    known = await repository.known_topics()
    system = SYSTEM + "\n\nЗаведённые сейчас темы:\n" + _topic_lines(known)

    runs = [await _run(model, entries, system) for _ in range(RUNS)]
    agreed = {
        entry.entry_id: _both(runs[0].get(entry.entry_id, []), runs[1].get(entry.entry_id, []))
        for entry in entries
    }

    result = await repository.replace_topics(
        agreed,
        vectors=await _vectors(embedder, agreed, known),
        embedding_model=embedder.model,
    )
    outcome = Regeneralized(
        entries=len(entries),
        topics_before=len(known),
        topics_after=result.topics_after,
        merged_away=result.removed,
    )
    logger.info(
        "переобобщение: записей %s, тем было %s, стало %s, убрано %s",
        outcome.entries,
        outcome.topics_before,
        outcome.topics_after,
        outcome.merged_away,
    )
    return outcome


async def _run(model: LanguageModel, entries: list[EntryText], system: str) -> dict[int, list[str]]:
    completion = await llm.complete_over_entries(
        model,
        entries=entries,
        system=system,
        schema=response_schema(),
        temperature=TEMPERATURE,
        purpose=PURPOSE,
    )
    expected = {entry.entry_id for entry in entries}
    parsed: dict[int, list[str]] = {}
    for raw in completion.data.get("items", []):
        try:
            entry_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("переобобщение не разобрано: %s", str(raw)[:200])
            continue
        if entry_id not in expected:
            logger.warning("модель вернула запись %s, которой в пачке не было", entry_id)
            continue
        parsed[entry_id] = [str(name) for name in raw.get("topics", []) if str(name).strip()]
    return parsed


def _both(first: list[str], second: list[str]) -> list[str]:
    """Тема засчитывается, только если её назвали оба прогона.

    То же правило, что у разметки (ADR-0011), и по той же причине: здесь оно
    не мешает бутстрапу, потому что пачка большая и обобщения в обоих прогонах
    получаются одинаковые — а вот случайная тема из одного прогона попала бы
    в список и прилипла.
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


def _topic_lines(known: list[repository.KnownTopic]) -> str:
    if not known:
        return "(тем ещё нет)"
    return "\n".join(f"- {topic.name}" for topic in known)


async def _vectors(
    embedder: Embedder, agreed: dict[int, list[str]], known: list[repository.KnownTopic]
) -> dict[str, list[float]]:
    """Вектора названий тем, которых ещё нет. Отказ провайдера прохода не рушит."""
    have = {topic.slug for topic in known}
    fresh = {normalize(name): name for names in agreed.values() for name in names}
    missing = [name for slug, name in fresh.items() if slug not in have]
    if not missing:
        return {}
    try:
        return dict(zip(missing, await embed_topic_names(embedder, missing), strict=True))
    except Exception:
        logger.exception("вектора новых тем не посчитались — темы заведутся без них")
        return {}


__all__ = [
    "BATCH",
    "EVERY",
    "PURPOSE",
    "Regeneralized",
    "due",
    "regeneralize",
]
