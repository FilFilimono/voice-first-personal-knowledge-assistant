"""Что должно работать (инвариант 5):

1. приватная запись не уходит в модель эмбеддингов — ни одного вызова наружу
2. но чанки у неё есть, и `tsv` заполнен: она находится словами
3. `embedding` у неё NULL — и это не «ещё не посчитали», а «и не будет»
4. проверка живёт внутри `core/retrieval`, а не в вызывающем коде

Пункт 4 — не педантизм. Проверка, вынесенная наверх, держится ровно до первого
нового вызывающего, который про неё не знал. Поэтому дверь наружу одна,
и предохранитель стоит в ней.

Приватная запись ищется хуже обычной — у неё нет одного канала из трёх.
Это прямое следствие ADR-0002, принятое там же и названное вслух.
"""

import pytest

import brain.core.retrieval.embeddings as embeddings
from brain.core.retrieval.errors import PrivateContentError
from tests.fakes import StubEmbedder


async def test_private_entry_never_reaches_the_provider() -> None:
    embedder = StubEmbedder()

    with pytest.raises(PrivateContentError):
        await embeddings.embed_entry_chunks(
            embedder,
            texts=["приватная мысль"],
            is_private=True,
        )

    assert embedder.calls == 0, "приватный текст ушёл наружу — инвариант 5 пробит"


async def test_private_entry_raises_instead_of_returning_nothing() -> None:
    """Пустой список — законный ответ на пустой ввод. Спутать нельзя."""
    embedder = StubEmbedder()

    assert await embeddings.embed_entry_chunks(embedder, texts=[], is_private=False) == []

    with pytest.raises(PrivateContentError):
        await embeddings.embed_entry_chunks(embedder, texts=[], is_private=True)


async def test_ordinary_entry_goes_through() -> None:
    embedder = StubEmbedder()

    vectors = await embeddings.embed_entry_chunks(
        embedder,
        texts=["первый кусок", "второй кусок"],
        is_private=False,
    )

    assert len(vectors) == 2
    assert embedder.documents == [["первый кусок", "второй кусок"]]


async def test_the_error_explains_itself() -> None:
    """Сообщение читает человек в логе, а не машина."""
    with pytest.raises(PrivateContentError) as error:
        await embeddings.embed_entry_chunks(StubEmbedder(), texts=["x"], is_private=True)

    message = str(error.value)
    assert "инвариант 5" in message
    assert "полнотекстом" in message, "не сказано, как приватная запись всё-таки ищется"
