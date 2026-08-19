"""Что должно работать: в колонку `chunks.embedding` ложится вектор единичной
длины, и на нём скалярное произведение равно косинусной близости.

**Почему тест устроен именно так — с двумя поправками к первоначальному замыслу.**

Поправка первая. Проверять «ненормированный вектор портит косинусную выдачу»
бессмысленно: `cos(a,b)` делит на нормы, и умножение вектора на 0.58 его
не меняет. Оператор `<=>` в pgvector делает то же самое. Такой тест не покраснел
бы никогда.

Поправка вторая, найденная замером, а не рассуждением. Второй замысел был
проверять порядок выдачи: «по `<#>` он должен совпадать с порядком по `<=>`».
Прогон на настоящих векторах — три текста, посчитанные обеими моделями, —
показал, что порядок совпадает **и на сырых векторах тоже**:
внутри одной модели нормы отличаются на 0.3%, а между моделями всё решает
близость, а не длина. Такой тест тоже был бы зелёным всегда.

Красным становится **равенство значений**, а не порядок:

    -(a <#> b)  ==  1 - (a <=> b)

Оно верно тогда и только тогда, когда оба вектора единичные. На сыром векторе
`gemini-embedding-001` (норма ≈0.58) самоблизость по скалярному произведению
равна 0.34 вместо 1.0 — и любой код, который считает «вектора нормированы,
значит скалярное произведение и есть косинус», молча получает треть правды.
Это и есть настоящая цена выключенной нормировки, и последний тест в файле
показывает её на записанном ответе провайдера.
"""

import json
import math
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.core.retrieval.gemini import GeminiEmbedder
from brain.storage.models.chunk import Chunk
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.types import to_literal

pytestmark = pytest.mark.integration

DATA = Path(__file__).parent / "data"

# Настоящие вектора обеих моделей на трёх **нейтральных** текстах. Из архива
# здесь нет ничего: нормы от содержания не зависят, а архив в git не попадает
# (ADR-0002). Файл пригодится и сверке `gemini-embedding-2` на eval'е.
PAIR: dict[str, Any] = json.loads((DATA / "neutral_embeddings.json").read_text())
RAW_VALUES: list[float] = PAIR["gemini-embedding-001"]["документы"]["погода"]


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _embedder(values: list[list[float]]) -> GeminiEmbedder:
    """Провайдер, отдающий записанные вектора. Настоящий разбор, без сети."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [{"values": v} for v in values]})

    return GeminiEmbedder(
        api_key="test-key",
        model="gemini-embedding-001",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _seed(url: str, vectors: list[list[float]]) -> list[int]:
    """Положить запись и её чанки с заданными векторами. Вернуть id чанков."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.TEXT,
                    raw_text="запись для проверки нормировки",
                    is_private=False,
                    timezone="Europe/Moscow",
                    dedup_key="test:нормировка",
                    meta={},
                )
                .returning(Entry.id)
            )
            ids: list[int] = []
            for ordinal, vector in enumerate(vectors):
                chunk_id = await connection.scalar(
                    insert(Chunk)
                    .values(
                        source_entry_id=entry_id,
                        ordinal=ordinal,
                        text=f"кусок {ordinal}",
                        embedding=vector,
                        embedding_model="gemini-embedding-001",
                    )
                    .returning(Chunk.id)
                )
                ids.append(int(chunk_id or 0))
            return ids
    finally:
        await engine.dispose()


async def _pairwise(url: str, ids: list[int]) -> list[tuple[float, float]]:
    """Для каждой пары чанков — косинусная близость и скалярное произведение."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT 1 - (a.embedding <=> b.embedding) AS cosine,
                           -(a.embedding <#> b.embedding)    AS dot
                    FROM chunks a CROSS JOIN chunks b
                    WHERE a.id = ANY(:ids) AND b.id = ANY(:ids)
                    """
                ),
                {"ids": ids},
            )
            return [(float(row[0]), float(row[1])) for row in result]
    finally:
        await engine.dispose()


async def test_what_lands_in_the_column_is_unit_length(migrated_database: str) -> None:
    """Самая прямая проверка: выключат нормировку — покраснеет сразу."""
    vectors = await _embedder([RAW_VALUES]).embed_documents(["нейтральный текст"])
    [chunk_id] = await _seed(migrated_database, vectors)

    engine = create_async_engine(migrated_database)
    try:
        async with engine.connect() as connection:
            stored = await connection.scalar(
                text("SELECT embedding FROM chunks WHERE id = :id"), {"id": chunk_id}
            )
    finally:
        await engine.dispose()

    values = [float(x) for x in str(stored).strip("[]").split(",")]
    assert _norm(values) == pytest.approx(1.0, abs=1e-6)


async def test_provider_sends_a_shorter_vector_than_it_should(migrated_database: str) -> None:
    """Оговорка вслух: нормировать приходится потому, что провайдер этого не делает."""
    assert _norm(RAW_VALUES) == pytest.approx(0.58, abs=0.02)


async def test_dot_product_equals_cosine_on_stored_vectors(migrated_database: str) -> None:
    """Равенство, верное только для единичных векторов.

    Именно на него опирается любой код, который считает близость скалярным
    произведением, — и именно оно ломается молча, если нормировку убрать.
    """
    raw = [PAIR["gemini-embedding-001"]["документы"][name] for name in ("погода", "техника")]
    vectors = await _embedder(raw).embed_documents(["погода", "техника"])
    ids = await _seed(migrated_database, vectors)

    for cosine, dot in await _pairwise(migrated_database, ids):
        assert dot == pytest.approx(cosine, abs=1e-5)


async def test_the_same_check_fails_on_raw_vectors(migrated_database: str) -> None:
    """Доказательство, что тест выше не пустой.

    Те же вектора, положенные как их прислал провайдер: самоблизость
    по скалярному произведению оказывается 0.34 вместо 1.0.
    """
    raw = [PAIR["gemini-embedding-001"]["документы"][name] for name in ("погода", "техника")]
    ids = await _seed(migrated_database, raw)

    pairs = await _pairwise(migrated_database, ids)

    assert any(abs(dot - cosine) > 0.5 for cosine, dot in pairs), (
        "на сырых векторах равенство не сломалось — проверка выше ничего не стоит"
    )


def test_vector_literal_survives_a_round_trip() -> None:
    """Перевод в строку и обратно не теряет знаков: иначе норма поедет сама."""
    from brain.storage.types import from_literal

    assert from_literal(to_literal(RAW_VALUES)) == RAW_VALUES
