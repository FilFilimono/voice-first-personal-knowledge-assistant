"""Связи: чем новая запись перекликается с прежними.

`vision.md` §5.4 и инвариант 8. Связь срабатывает **в момент записи**, а не
по расписанию, и считается **чистой векторной близостью, без вызова модели**.
Причина названа там же и она не про экономию токенов вообще: связи —
единственный механизм, который срабатывает на каждой записи, и вызов модели
на нём оплачивался бы каждый день, а не изредка.

Отсюда устройство модуля: один запрос к `chunks` с готовыми векторами и четыре
правила отбора. Никакого переранжирования, никакого «умного» подбора.

Правила, и каждое куплено прогоном архива 27.08.2026, а не рассуждением:

    порог             сколько косинуса считать перекличкой. `vision.md`
                      называет ~0.75 — на живом архиве это 94% записей
                      и 18 кандидатов на запись, то есть «связано всё
                      со всем». Число настроечное, выбирает владелец;
    возраст           кандидат обязан быть старше на сутки. Без этого треть
                      связей приходилась на тот же день, а сегодняшнее
                      владелец помнит и без бота;
    сама запись       ни она, ни её соседний чанк в кандидаты не попадают.
                      Это следует из возрастного зазора, но проверяется
                      отдельно: правило «не показывать себя» не должно
                      держаться на побочном действии другого правила;
    приватность       приватная запись целью не бывает. У неё нет вектора
                      по построению (инвариант 5), но условие стоит явно:
                      механизм, полагающийся на «у них и так нет вектора»,
                      сломается молча в тот день, когда вектор появится.

**Тип цели.** Фраза связи звучит «а кстати, что ты думал про…», и указывать
ею на напоминание «уйти домой в 19:40» бессмысленно. Поэтому целью может быть
только размышление — правило включается настройкой `LINKS_TARGET_THINKING`.
Тип **источника** при этом не проверяется вовсе, и не по недосмотру: связь
считается сразу после индексации, а разметка приходит секунд через шестьдесят
(`core/scheduler/jobs`), то есть в этот момент тип новой записи ещё неизвестен.

**Порядок работы с приватностью.** Приватная запись не получает связей вовсе:
её вектора не существует, и спрашивать нечего. Это не отдельная ветка кода,
а следствие — и проверяется тестом, потому что следствия ломаются тише правил.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Float, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config import Settings
from brain.core.memory import overrides
from brain.core.memory.repository import display_text, is_private_now
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.chunk import DIMENSIONS, Chunk
from brain.storage.models.entry import Entry
from brain.storage.types import Vector

logger = logging.getLogger(__name__)

# Типы, на которые связь имеет право указывать. «Что ты думал про…» — это
# вопрос к размышлению; задача, событие и мусор ответом на него не являются.
# Петля здесь тоже размышление: это вопрос, на который владелец не ответил.
THINKING: frozenset[AnnotationType] = frozenset(
    {
        AnnotationType.THOUGHT,
        AnnotationType.INSIGHT,
        AnnotationType.DECISION,
        AnnotationType.LOOP,
    }
)

# Сколько кандидатов берётся из базы до отбора по типу. Больше, чем показываем,
# намеренно: у первых по близости тип может оказаться неподходящим, и тогда
# связь должна найтись у следующих, а не пропасть.
CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class Rules:
    """Чем связь отличается от совпадения. Числа приходят из настроек.

    Отдельным значением, а не пятью параметрами: правила читаются вместе
    и меняются вместе, а расползшись по подписи, они первым делом разъедутся
    между вызывающим кодом и замером.
    """

    threshold: float
    limit: int
    min_age: timedelta
    only_thinking: bool


def from_settings(settings: Settings) -> Rules:
    """Правила из настроек. Единственное место, где числа берутся из `.env`."""
    return Rules(
        threshold=settings.links_threshold,
        limit=settings.links_max,
        min_age=timedelta(days=settings.links_min_age_days),
        only_thinking=settings.links_target_thinking,
    )


@dataclass(frozen=True, slots=True)
class Link:
    """Прежняя запись, с которой перекликается новая."""

    entry_id: int
    # Текст **вычищенный** (`display_text()`), а не текст чанка: показывается
    # он владельцу, а показ везде идёт по вычищенному (ADR-0015, ось 3).
    text: str
    created_at: datetime
    timezone: str
    similarity: float


async def related(entry_id: int, *, rules: Rules) -> list[Link]:
    """С чем перекликается запись. Пусто — перекликаться не с чем.

    Пустой ответ — обычный исход, а не сбой: на архиве в 85 записей связь
    находится у каждой пятой (при пороге 0.85 и суточном зазоре). Молчание
    здесь честнее натянутой связи: связь, показанная на пустом месте, стоит
    доверия ко всем остальным.
    """
    entry = await _entry(entry_id)
    if entry is None:
        logger.info("связи не считаются: записи %s нет", entry_id)
        return []

    created_at, vectors = entry
    if not vectors:
        # Либо запись приватна (вектора нет — инвариант 5), либо её ещё
        # не проиндексировали. Оба состояния законные.
        return []

    cutoff = created_at - rules.min_age
    found: dict[int, float] = {}
    async with session_scope() as session:
        for vector in vectors:
            for candidate_id, similarity in await _closest(
                session, vector, entry_id=entry_id, cutoff=cutoff, threshold=rules.threshold
            ):
                # У записи из нескольких чанков лучший чанк и решает: связь
                # между записями, а не между их кусками.
                found[candidate_id] = max(found.get(candidate_id, 0.0), similarity)

    ranked = sorted(found.items(), key=lambda item: (-item[1], item[0]))
    chosen = await _pick(ranked, rules=rules)
    return await _load(chosen)


async def _entry(entry_id: int) -> tuple[datetime, list[list[float]]] | None:
    """Момент записи и вектора её чанков. `None` — записи нет.

    Момент берётся у самой записи, а не `now`: возрастной зазор меряется от неё.
    Перепрогон индекса недельной давности не должен вдруг считать соседей
    «моложе суток».
    """
    async with session_scope() as session:
        created_at = await session.scalar(select(Entry.created_at).where(Entry.id == entry_id))
        if created_at is None:
            return None
        vectors = (
            await session.scalars(
                select(Chunk.embedding)
                .where(Chunk.source_entry_id == entry_id, Chunk.embedding.is_not(None))
                .order_by(Chunk.ordinal)
            )
        ).all()
    return created_at, [list(vector) for vector in vectors if vector is not None]


async def _closest(
    session: AsyncSession,
    vector: list[float],
    *,
    entry_id: int,
    cutoff: datetime,
    threshold: float,
) -> list[tuple[int, float]]:
    """Ближайшие записи к одному вектору. Отбор целиком в SQL.

    `<=>` — косинусное расстояние pgvector, поэтому близость это `1 - <=>`.
    Ровно тот же оператор, что в векторном канале поиска (`search.py`), и это
    не совпадение: связь и поиск обязаны одинаково понимать слово «похоже».
    """
    # `return_type` обязателен, и это не украшение подписи. Без него SQLAlchemy
    # считает результат `<=>` вектором — по типу операндов — и единицу в `1 - …`
    # тоже готовит как вектор: `float(1)` не итерируется, запрос падает
    # `TypeError` ещё до базы. Падал он внутри `try` в `jobs.index`, то есть
    # связи молчали, а в журнале лежало «связи не показаны». Найдено первым же
    # тестом на живых векторах 01.09.2026.
    distance = Chunk.embedding.op("<=>", return_type=Float)(literal(vector, Vector(DIMENSIONS)))
    best = func.max(1 - distance).label("similarity")

    rows = await session.execute(
        select(Chunk.source_entry_id, best)
        .join(Entry, Entry.id == Chunk.source_entry_id)
        .where(
            Chunk.embedding.is_not(None),
            # Ни сама запись, ни её соседний чанк: связь на самого себя — самая
            # очевидная и самая незаметная ошибка этого механизма.
            Chunk.source_entry_id != entry_id,
            Entry.created_at <= cutoff,
            # Приватность спрашивается **сейчас**, а не колонкой: её включают
            # задним числом, и это единственное место, где вопрос решается.
            ~is_private_now(),
        )
        .group_by(Chunk.source_entry_id)
        .having(best >= threshold)
        .order_by(best.desc(), Chunk.source_entry_id)
        .limit(CANDIDATES)
    )
    return [(int(row[0]), float(row[1])) for row in rows]


async def _pick(ranked: list[tuple[int, float]], *, rules: Rules) -> list[tuple[int, float]]:
    """Оставить те связи, которые имеет смысл показывать.

    Тип цели спрашивается по одной записи, а не выражением в общем запросе,
    и это намеренно: «чем запись считается сейчас» решает `overrides
    .current_type()` — единственное место, где поправка владельца перебивает
    ответ модели. Второе такое выражение рядом однажды разъехалось бы с ним
    молча, а кандидатов здесь не больше десяти.
    """
    chosen: list[tuple[int, float]] = []
    for candidate_id, similarity in ranked:
        if len(chosen) == rules.limit:
            break
        if rules.only_thinking and await overrides.current_type(candidate_id) not in THINKING:
            continue
        chosen.append((candidate_id, similarity))
    return chosen


async def _load(chosen: list[tuple[int, float]]) -> list[Link]:
    """Добрать то, что показывают владельцу: дату, таймзону и текст."""
    if not chosen:
        return []

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, display_text(), Entry.created_at, Entry.timezone).where(
                    Entry.id.in_([entry_id for entry_id, _ in chosen])
                )
            )
        ).all()

    by_id = {int(row[0]): row for row in rows}
    links: list[Link] = []
    for entry_id, similarity in chosen:
        row = by_id.get(entry_id)
        if row is None or row[1] is None:  # pragma: no cover — запись только что читалась
            logger.info("связь на запись %s без текста — пропускаю", entry_id)
            continue
        links.append(
            Link(
                entry_id=entry_id,
                text=str(row[1]),
                created_at=row[2],
                timezone=str(row[3]),
                similarity=similarity,
            )
        )
    return links
