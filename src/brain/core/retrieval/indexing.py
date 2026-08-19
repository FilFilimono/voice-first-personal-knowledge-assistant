"""Превращение записи в то, по чему можно искать.

Три шага, и порядок важен: взять текущий текст → разрезать → посчитать вектора.
Текст берётся «текущий», а не «сырой», потому что у голосовой записи своего
текста нет вовсе — он приходит расшифровкой через секунды после приёма.

**Почему индексация — задание в очереди, а не работа при приёме.** Расчёт
эмбеддинга это сетевой вызов: он падает, он ждёт, он стоит денег. Подтверждение
приёма должно уйти быстрее 300 мс. Ровно та же причина, по которой в отдельном
задании живёт расшифровка, и ровно поэтому задание ставится **в транзакции
приёма** (инвариант 7): запись без задания означала бы запись, которую никогда
не найдут, и узнать об этом было бы неоткуда.

**Приватная запись индексируется тоже.** У неё есть чанки и `tsv`, поэтому она
находится словами; наружу не уходит только вектор (инвариант 5). Пропустить её
целиком значило бы сделать приватные записи ненаходимыми вовсе — а ADR-0002
обещает другое.
"""

import logging

from brain.core.memory import repository as memory
from brain.core.ports import Embedder
from brain.core.retrieval.chunking import split_entry
from brain.core.retrieval.embeddings import embed_entry_chunks
from brain.core.retrieval.repository import replace_chunks

logger = logging.getLogger(__name__)


async def index_entry(embedder: Embedder, *, entry_id: int) -> int:
    """Разрезать запись и посчитать вектора. Вернуть число чанков.

    Ноль означает «индексировать нечем»: у голосовой записи ещё нет
    расшифровки. Это законное состояние, а не отказ — задание придёт снова,
    когда расшифровка появится, и ставит его тот же код, что её сохраняет.
    """
    found = await memory.entry_text(entry_id)
    if found is None:
        logger.info("индексировать нечего: у записи %s ещё нет текста", entry_id)
        return 0

    text, is_private = found
    chunks = split_entry(entry_id, text)
    if not chunks:
        return 0

    vectors: list[list[float] | None]
    if is_private:
        # Наружу не ходим вовсе. Предохранитель в `embed_entry_chunks` тоже
        # есть, но он на случай ошибки, а не рабочий путь: приватная запись
        # не должна доходить до двери и стучаться в неё.
        vectors = [None] * len(chunks)
        model = None
    else:
        vectors = list(await embed_entry_chunks(embedder, texts=chunks, is_private=False))
        model = embedder.model

    return await replace_chunks(entry_id, chunks, vectors, model=model)
