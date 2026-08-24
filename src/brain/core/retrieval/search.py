"""Гибридный поиск: три канала и слияние через RRF.

Три канала ошибаются по-разному, и в этом весь смысл (ADR-0006):

  полнотекст   точные термины и словоформы. Русский snowball сводит «напомни»
               и «напоминание» к «напомн», но английские слова внутри русской
               речи оставляет как есть;
  триграммы    опечатки расшифровки и имена собственные. Именно этот канал
               находит «PGVector», написанный моделью как попало;
  вектора      смысл. Единственный, кто находит запись, не разделяющую
               с запросом ни одного слова.

**Про «BM25».** В плане и ADR канал полнотекста назван BM25. Настоящего BM25
в ядре Postgres нет: `ts_rank_cd` считает по-другому — плотность вхождений
с учётом расстояния между словами, без насыщения по частоте и без нормировки
на длину документа. Для RRF это почти безразлично, потому что в слияние уходят
**ранги, а не веса**, но называть вещи чужими именами в коде не стоит: канал
здесь так и зовётся — `fulltext`.

**Про слияние.** RRF: `score = Σ wᵢ / (k + rankᵢ)`. Ранг, а не оценка,
намеренно — оценки трёх каналов несопоставимы (косинус от 0 до 1, `ts_rank_cd`
без верхней границы), и любая попытка привести их к общей шкале была бы
подгонкой. Константа k = 60 — из исходной работы про RRF; она сглаживает
разницу между первым и вторым местом, чтобы один канал не решал всё.

**Веса равные.** Это стартовое положение, а не результат замера: любой другой
набор был бы предпочтением, выданным за знание. Правятся по eval-набору
(`evals/retrieval`), где recall считается ещё и по каждому каналу отдельно —
без поканальных чисел непонятно, какой канал свой вес отрабатывает.

Слияние делается в Python, а не одним SQL с тремя CTE. Одним SQL это пишется
и работает, но через полгода владелец должен уметь объяснить голосом именно
слияние — а не разбирать три вложенных `row_number() OVER ()`.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.core.memory.repository import display_text, is_private_now
from brain.core.ports import Embedder
from brain.storage.db import session_scope
from brain.storage.models.chunk import FTS_CONFIG
from brain.storage.models.entry import Entry
from brain.storage.types import to_literal

logger = logging.getLogger(__name__)

FULLTEXT = "fulltext"
TRIGRAM = "trigram"
VECTOR = "vector"
CHANNELS = (FULLTEXT, TRIGRAM, VECTOR)

# Стартовые веса — равные. Меняются по числу из eval-набора, не по ощущению.
WEIGHTS: dict[str, float] = {FULLTEXT: 1.0, TRIGRAM: 1.0, VECTOR: 1.0}

# Константа RRF из исходной работы. Сглаживает разрыв между первым и вторым
# местом: без неё канал, уверенно ошибшийся на первом месте, тянул бы выдачу.
RRF_K = 60

# Сколько кандидатов берётся из каждого канала до слияния. Больше выдачи
# намеренно: запись, стоящая у одного канала десятой, после слияния может
# оказаться первой — ради этого RRF и нужен.
CANDIDATES = 20

# Порог похожести для триграмм. Умолчание pg_trgm (0.6) рассчитано на сверку
# двух коротких строк; у нас короткий запрос против чанка в сотни символов,
# и на умолчании канал молчит почти всегда.
TRIGRAM_THRESHOLD = 0.3


@dataclass(frozen=True, slots=True)
class Found:
    """Найденная запись: чем она была, когда сказана и почему найдена."""

    entry_id: int
    created_at: datetime
    timezone: str
    is_private: bool
    # Начало записи, **вычищенное** (`display_text()`), а не текст чанка.
    #
    # Чанки строятся над рабочим текстом, то есть пока над дословным. Оставь
    # выдача текст чанка — поиск остался бы единственным экраном, где владелец
    # продолжает видеть «э-э-э», а сказано было «везде без исключений»
    # (ADR-0015, ось 3; найдено ревизией Задачи 1).
    #
    # **Цена названа вслух.** У записи длиннее 1500 символов чанков несколько,
    # и показанный кусок перестаёт быть тем куском, который совпал с запросом,
    # — показывается начало записи. Сегодня это задевает единицы записей
    # (медиана 409 символов, максимум 1958), но с ростом архива будет задевать
    # чаще. Настоящее решение — вычищенные чанки, и оно приходит вместе
    # с переключением рабочего текста, а не раньше.
    text: str
    score: float
    # Какие каналы её нашли. Нужно и eval'у, и объяснимости: «нашлось только
    # вектором» и «нашлось всеми тремя» — разные степени уверенности.
    channels: tuple[str, ...]


async def search(embedder: Embedder, query: str, *, limit: int = 5) -> list[Found]:
    """Найти записи по запросу. Пустой запрос ничего не ищет."""
    query = query.strip()
    if not query:
        return []

    rankings = await channel_rankings(embedder, query)
    merged = merge(rankings)
    return await _load(merged[:limit], rankings)


async def channel_rankings(embedder: Embedder, query: str) -> dict[str, list[int]]:
    """Что нашёл каждый канал — списком записей по убыванию его уверенности.

    Отдельной функцией, потому что этим пользуется eval: без поканальных чисел
    правка весов возвращается к «мне кажется, вектор надо усилить».
    """
    vector = await embedder.embed_query(query)

    async with session_scope() as session:
        # SET LOCAL живёт до конца транзакции и не трогает другие соединения.
        await session.execute(
            text(f"SET LOCAL pg_trgm.word_similarity_threshold = {TRIGRAM_THRESHOLD}")
        )

        return {
            FULLTEXT: await _entries(session, _FULLTEXT_SQL, {"q": query}),
            TRIGRAM: await _entries(session, _TRIGRAM_SQL, {"q": query}),
            VECTOR: await _entries(session, _VECTOR_SQL, {"q": to_literal(vector)}),
        }


def merge(rankings: dict[str, list[int]], weights: dict[str, float] | None = None) -> list[int]:
    """Слить ранжирования каналов в один список. Это и есть RRF.

    Вся формула — три строки ниже. Ранг считается с единицы: у первого места
    вклад `w / 61`, у двадцатого — `w / 80`, то есть разница между каналами,
    нашедшими запись, всегда больше разницы между местами внутри канала.
    """
    weights = weights or WEIGHTS
    scores: dict[int, float] = {}

    for channel, entry_ids in rankings.items():
        weight = weights.get(channel, 0.0)
        for rank, entry_id in enumerate(entry_ids, start=1):
            scores[entry_id] = scores.get(entry_id, 0.0) + weight / (RRF_K + rank)

    return sorted(scores, key=lambda entry_id: (-scores[entry_id], entry_id))


def score_of(rankings: dict[str, list[int]], entry_id: int) -> tuple[float, tuple[str, ...]]:
    """Оценка записи и каналы, которые её нашли."""
    score = 0.0
    channels: list[str] = []
    for channel, entry_ids in rankings.items():
        if entry_id in entry_ids:
            channels.append(channel)
            score += WEIGHTS.get(channel, 0.0) / (RRF_K + entry_ids.index(entry_id) + 1)
    return score, tuple(channel for channel in CHANNELS if channel in channels)


# Полнотекст. Слова запроса соединяются через **ИЛИ**, а не через И.
#
# Это не мелочь, и найдено оно замером, а не рассуждением. `websearch_to_tsquery`
# и `plainto_tsquery` соединяют слова через И, то есть требуют присутствия
# каждого. На вопросе «что я решил про поиск и фазу два» это даёт
# `'реш' & 'поиск' & 'фаз'` — и запись 31, где есть «поиск» и «фаза», но нет
# «решил», не находится вовсе. Первый прогон eval показал recall полнотекста
# **0.00 на всех вопросах**: канал был мёртвым грузом ровно там, где архив
# и нужен, — на живом вопросе, а не на поиске точного слова.
#
# ИЛИ — это и есть поведение BM25, на которое ссылается ADR-0006: он ранжирует
# документы, содержащие **любое** из слов, а не фильтрует по всем сразу.
# И-семантика это фильтр, а не ранжирование.
#
# Запрос собирается из лексем, которые вернул сам Postgres, и каждая обёрнута
# в `quote_literal` — своего разбора пользовательского ввода здесь нет,
# и подставить в `to_tsquery` что-нибудь своё нельзя.
_FULLTEXT_SQL = f"""
    SELECT c.source_entry_id, c.id
    FROM chunks c
    CROSS JOIN LATERAL (
        SELECT to_tsquery('{FTS_CONFIG}', string_agg(quote_literal(lexeme), ' | ')) AS q
        FROM unnest(tsvector_to_array(to_tsvector('{FTS_CONFIG}', :q))) AS lexeme
    ) query
    WHERE c.tsv @@ query.q
    ORDER BY ts_rank_cd(c.tsv, query.q) DESC, c.id
    LIMIT {CANDIDATES}
"""

# Триграммы. `word_similarity(:q, text)` меряет запрос против **лучшего куска**
# текста, а не против всей строки целиком. Обычная `similarity` на коротком
# запросе и чанке в 900 символов близка к нулю у всех кандидатов сразу,
# и канал не работает вовсе — это разница между рабочим каналом и украшением.
_TRIGRAM_SQL = f"""
    SELECT c.source_entry_id, c.id
    FROM chunks c
    WHERE :q <% c.text
    ORDER BY word_similarity(:q, c.text) DESC, c.id
    LIMIT {CANDIDATES}
"""

# Вектора. `<=>` — косинусное расстояние; приватные записи сюда не попадают
# по построению, у них `embedding IS NULL` (инвариант 5).
_VECTOR_SQL = f"""
    SELECT c.source_entry_id, c.id
    FROM chunks c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> :q, c.id
    LIMIT {CANDIDATES}
"""


async def _entries(session: AsyncSession, sql: str, params: dict[str, str]) -> list[int]:
    """Выполнить запрос канала и вернуть записи без повторов, сохранив порядок.

    Два чанка одной записи — это одна запись в выдаче, а не две. Запись
    попадает на то место, где стоял её лучший чанк.
    """
    result = await session.execute(text(sql), params)

    seen: list[int] = []
    for entry_id, _chunk_id in result:
        if entry_id not in seen:
            seen.append(int(entry_id))
    return seen


async def _load(entry_ids: Sequence[int], rankings: dict[str, list[int]]) -> list[Found]:
    """Добрать то, что показывают владельцу: дату, таймзону и начало текста."""
    if not entry_ids:
        return []

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Entry.id,
                    Entry.created_at,
                    Entry.timezone,
                    # Замочек в выдаче обязан показывать нынешнее состояние:
                    # запись, сделанную приватной задним числом, колонка
                    # по-прежнему считает обычной (инвариант 3).
                    is_private_now(),
                    # То, что видит владелец, — вычищенный текст, если он есть.
                    # Джойна с чанками здесь больше нет: он был нужен только
                    # ради текста нулевого чанка.
                    display_text(),
                ).where(Entry.id.in_(list(entry_ids)))
            )
        ).all()

    by_id = {int(row[0]): row for row in rows}
    found: list[Found] = []
    for entry_id in entry_ids:
        row = by_id.get(entry_id)
        if row is None:
            # Канал нашёл запись, а её нулевого чанка уже нет: между поиском
            # и чтением прошёл пересчёт. Пропускаем, сказав об этом в лог —
            # показать на одну запись меньше лучше, чем показать пустую строку.
            logger.info("запись %s нашлась, но чанка нет — пересчёт на ходу?", entry_id)
            continue
        score, channels = score_of(rankings, entry_id)
        found.append(
            Found(
                entry_id=entry_id,
                created_at=row[1],
                timezone=str(row[2]),
                is_private=bool(row[3]),
                text=str(row[4]),
                score=score,
                channels=channels,
            )
        )
    return found
