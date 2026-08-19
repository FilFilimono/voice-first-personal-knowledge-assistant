"""Что должно работать — на живой базе и на живом архиве:

1. находит по смыслу, когда общих слов с запросом нет вовсе
2. находит по имени собственному, которое расшифровка написала как попало
3. находит по точной формулировке
4. приватная запись находится словами, но эмбеддинга у неё нет (инвариант 5)
5. RRF складывает каналы, а не заменяет их: запись, найденная двумя каналами,
   обгоняет запись, найденную одним

**Данные — весь архив целиком**, прочитанный из живой базы при прогоне.
В репозитории его нет и не будет: архив наружу не уходит, и git это тоже
наружу (ADR-0002). Нет базы — тест скипается с объяснением.

Вектора настоящие: посчитаны той самой моделью и лежат в игнорируемом git'ом
кэше рядом. Заглушка с хэшом от текста проверяет SQL и молчит про то, работает
ли поиск, — а именно это и надо знать.

Тесты привязаны к номерам записей, а не к их тексту: текст живёт в базе,
и дублировать его сюда значило бы вернуть архив в репозиторий.
"""

from typing import Any

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings
from brain.core.retrieval import search
from brain.core.retrieval.chunking import split
from brain.core.retrieval.indexing import index_entry
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from tests import archive as archive_module
from tests.fakes import RecordedEmbedder

pytestmark = pytest.mark.integration

# Записи архива, на которых держатся проверки ниже. Только номера: сам текст
# лежит в базе, и переносить его сюда значило бы вернуть архив в git.
DISCIPLINE = 1  # «Завтра у меня CV чек…» — про несделанное и дисциплину
SEARCH_PHASE = 31  # длинная запись про фазу два, PGVector и продажу бота

# Запрос без единого общего слова с записью 1. Первый вариант — «как я себя
# чувствую последние дни» — был отброшен проверкой ниже: он делит с записью
# слово «последние», и тест перестал бы что-либо доказывать про вектора.
MEANING_QUERY = "чего я не успеваю"

QUERIES = [MEANING_QUERY, "PGVector", "CV чек"]


@pytest.fixture
async def indexed(migrated_database: str, archive: list[archive_module.Entry]) -> RecordedEmbedder:
    """Живой архив в одноразовой базе, проиндексированный настоящим кодом."""
    vectors = await archive_module.vectors(
        documents=[chunk for entry in archive for chunk in split(entry.text)],
        queries=QUERIES,
    )
    if vectors is None:
        pytest.skip(archive_module.NO_VECTORS)

    embedder = RecordedEmbedder(vectors, get_settings().embedding_model)

    engine = create_async_engine(migrated_database)
    try:
        async with engine.begin() as connection:
            for entry in archive:
                await connection.execute(
                    insert(Entry).values(
                        id=entry.id,
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind(entry.kind),
                        raw_text=entry.text,
                        is_private=False,
                        timezone=entry.timezone,
                        dedup_key=f"archive:{entry.id}",
                        meta={},
                    )
                )
            await connection.execute(
                text("SELECT setval('entries_id_seq', (SELECT max(id) FROM entries))")
            )
    finally:
        await engine.dispose()

    for entry in archive:
        await index_entry(embedder, entry_id=entry.id)

    return embedder


def _ids(found: list[search.Found]) -> list[int]:
    return [item.entry_id for item in found]


async def test_the_archive_is_really_indexed(indexed: RecordedEmbedder) -> None:
    """Оговорка вслух: без этого все проверки ниже проверяли бы пустую таблицу."""
    found = await search.search(indexed, "PGVector", limit=5)

    assert found, "поиск не нашёл ничего — архив не проиндексировался"


# --- Смысл ---


async def test_finds_by_meaning_without_shared_words(indexed: RecordedEmbedder) -> None:
    """Главное, ради чего вообще нужны вектора.

    В записи 1 нет ни «успеваю», ни «чего»: там «расстраивает», «не получается
    нормально работать», «ничего не делаю». Ни полнотекст, ни триграммы такую
    связь не увидят — им не за что зацепиться.
    """
    found = await search.search(indexed, MEANING_QUERY, limit=5)

    assert DISCIPLINE in _ids(found)


async def test_that_query_really_shares_no_words(
    indexed: RecordedEmbedder, migrated_database: str
) -> None:
    """Проверка проверки: пересекись лексемы — и тест выше ничего не значит.

    Считается базой, а не глазами: «дни» и «дней» это одна лексема «дн»,
    и на глаз такое пересечение не видно.
    """
    engine = create_async_engine(migrated_database)
    try:
        async with engine.connect() as connection:
            shared = await connection.scalar(
                text(
                    """
                    SELECT array(
                        SELECT unnest(tsvector_to_array(to_tsvector('russian', :q)))
                        INTERSECT
                        SELECT unnest(tsvector_to_array(to_tsvector('russian', c.text)))
                    )
                    FROM chunks c WHERE c.source_entry_id = :id
                    """
                ),
                {"q": MEANING_QUERY, "id": DISCIPLINE},
            )
    finally:
        await engine.dispose()

    assert shared == [], f"общие лексемы есть: {shared} — вектор тут ни при чём"


async def test_the_word_channel_cannot_find_it(indexed: RecordedEmbedder) -> None:
    """И это видно поканально: полнотексту не за что зацепиться.

    Триграммы при этом слабо срабатывают — 0.333 при пороге 0.3, — но это
    совпадение обрывков, а не находка: общих слов в записи нет ни одного
    (тест выше считает это лексемами базы). Порог триграмм, как и веса RRF,
    — стартовое положение, и уточняется он по eval-набору, а не по одному
    пограничному случаю: подгонка порога под удобный пример это ровно то,
    от чего защищает правило «править по числу, а не по ощущению».
    """
    rankings = await search.channel_rankings(indexed, MEANING_QUERY)

    assert DISCIPLINE in rankings[search.VECTOR]
    assert DISCIPLINE not in rankings[search.FULLTEXT]


# --- Точные слова ---


async def test_finds_by_proper_noun(indexed: RecordedEmbedder) -> None:
    """«PGVector» — имя собственное внутри русской речи, и оно одно на архив."""
    found = await search.search(indexed, "PGVector", limit=5)

    assert _ids(found)[0] == SEARCH_PHASE


async def test_finds_by_exact_wording(indexed: RecordedEmbedder) -> None:
    """«CV чек» сказано в записи 1 ровно так и больше нигде."""
    found = await search.search(indexed, "CV чек", limit=5)

    assert DISCIPLINE in _ids(found)


async def test_word_channels_carry_the_proper_noun(indexed: RecordedEmbedder) -> None:
    """Имя собственное берут слова, а не смысл: без этих каналов оно теряется."""
    rankings = await search.channel_rankings(indexed, "PGVector")

    assert SEARCH_PHASE in rankings[search.FULLTEXT] or SEARCH_PHASE in rankings[search.TRIGRAM]


# --- Приватность ---


async def test_private_entry_is_found_by_words(
    indexed: RecordedEmbedder, migrated_database: str
) -> None:
    """Инвариант 5 не делает запись невидимой — только беззвучной для модели.

    Проверяется **попадание в словесные каналы**, а не место в общей выдаче.
    Прежняя редакция требовала, чтобы запись была в топ-5 по запросу «CV чек»,
    и покраснела 20 августа 2026 — не от поломки, а от того, что архив дорос
    с 39 записей до 50 и запись 47 оттеснила первую. Утверждение про место
    в выдаче на растущем архиве нестабильно по построению, а инвариант,
    ради которого тест написан, к месту в выдаче отношения не имеет.
    """
    await _make_private(migrated_database, DISCIPLINE)
    await index_entry(indexed, entry_id=DISCIPLINE)

    rankings = await search.channel_rankings(indexed, "CV чек")

    assert DISCIPLINE in rankings[search.FULLTEXT] or DISCIPLINE in rankings[search.TRIGRAM], (
        "приватная запись потерялась и в словесных каналах — искать её нечем"
    )


async def test_private_entry_has_no_embedding(
    indexed: RecordedEmbedder, migrated_database: str
) -> None:
    await _make_private(migrated_database, DISCIPLINE)
    await index_entry(indexed, entry_id=DISCIPLINE)

    assert await _vectors(migrated_database, DISCIPLINE) == [None]


async def test_private_entry_keeps_its_fulltext(
    indexed: RecordedEmbedder, migrated_database: str
) -> None:
    """`tsv` считает база, поэтому он есть даже там, где вектора нет."""
    await _make_private(migrated_database, DISCIPLINE)
    await index_entry(indexed, entry_id=DISCIPLINE)

    engine = create_async_engine(migrated_database)
    try:
        async with engine.connect() as connection:
            tsv = await connection.scalar(
                text("SELECT tsv FROM chunks WHERE source_entry_id = :id"), {"id": DISCIPLINE}
            )
    finally:
        await engine.dispose()

    assert tsv, "у приватной записи пустой полнотекст — искать её нечем"


async def test_private_entry_drops_out_of_the_vector_channel(
    indexed: RecordedEmbedder, migrated_database: str
) -> None:
    await _make_private(migrated_database, DISCIPLINE)
    await index_entry(indexed, entry_id=DISCIPLINE)

    rankings = await search.channel_rankings(indexed, MEANING_QUERY)

    assert DISCIPLINE not in rankings[search.VECTOR]


# --- Слияние ---


async def test_two_channels_beat_one() -> None:
    """RRF складывает каналы. Это чистая арифметика, база не нужна."""
    rankings = {
        search.FULLTEXT: [10, 20],
        search.TRIGRAM: [20],
        search.VECTOR: [],
    }

    assert search.merge(rankings)[0] == 20, "запись, найденную дважды, обогнала найденная раз"


async def test_merge_is_stable_on_ties() -> None:
    """Одинаковые оценки не должны менять порядок от запуска к запуску."""
    rankings = {search.FULLTEXT: [7, 3], search.TRIGRAM: [3, 7], search.VECTOR: []}

    assert search.merge(rankings) == search.merge(rankings)


async def test_channels_are_visible_in_the_result(indexed: RecordedEmbedder) -> None:
    """Понимать, чем найдено, нужно и eval'у, и владельцу."""
    found = await search.search(indexed, "PGVector", limit=5)

    assert found[0].channels
    assert set(found[0].channels) <= set(search.CHANNELS)


async def test_empty_query_finds_nothing(indexed: RecordedEmbedder) -> None:
    assert await search.search(indexed, "   ") == []


# --- Вспомогательное ---


async def _make_private(url: str, entry_id: int) -> None:
    """Пометить запись приватной.

    Прямым UPDATE, в обход инварианта 3, — намеренно и только в тесте: штатный
    путь (`entry_overrides`) появится в Фазе 3, а проверять поведение приватной
    записи надо уже сейчас. Триггер `entries` это запрещает, поэтому он тут
    временно снимается.
    """
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE entries DISABLE TRIGGER USER"))
            await connection.execute(
                update(Entry).where(Entry.id == entry_id).values(is_private=True)
            )
            await connection.execute(text("ALTER TABLE entries ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


async def _vectors(url: str, entry_id: int) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT embedding FROM chunks WHERE source_entry_id = :id ORDER BY ordinal"),
                {"id": entry_id},
            )
            return [row[0] for row in result]
    finally:
        await engine.dispose()
