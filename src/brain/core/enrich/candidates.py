"""Отбор тем-кандидатов, когда весь список уже не помещается в промпт.

Включается на полутора сотнях тем (`service.TOPIC_LIST_LIMIT`). До того момента
модель видит список целиком — это дешевле сотни токенов и накрывает 100% тем,
тогда как отбор по близости накрывает 84% при десяти кандидатах на запись
(замер 19.08.2026, [ADR-0011](../../../../docs/adr/0011-annotation-types-state-and-topics.md)).

**Почему это написано сейчас, а не в тот день, когда понадобится.** В тот день
никто не будет разбираться, почему разметка вдруг начала заводить дубли тем:
она просто перестанет попадать в существующие, и заметить это можно будет
только по разросшемуся списку. Механика, включающаяся сама, обязана быть
написана и проверена заранее — тест гоняет её, опустив порог.

Вектор записи берётся у её нулевого чанка: он уже посчитан индексацией,
и считать его второй раз значило бы платить за то же самое дважды.
"""

from sqlalchemy import bindparam, text

from brain.core.enrich.repository import KnownTopic
from brain.storage.db import session_scope

# Сколько кандидатов на запись. Десять — из замера: top-10 накрывает 84%,
# top-5 только 70%, а стоит вдвое меньше токенов, которых и так не жалко.
PER_ENTRY = 10

# Кандидаты берутся на каждую запись пачки отдельно и объединяются: у пачки
# нет общего смысла, это просто то, что накопилось за несколько минут.
SQL = text(
    """
    SELECT DISTINCT nearest.id
    FROM (
        SELECT embedding
        FROM chunks
        WHERE source_entry_id = ANY(:entry_ids) AND ordinal = 0 AND embedding IS NOT NULL
    ) AS entry
    CROSS JOIN LATERAL (
        SELECT id
        FROM topics
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> entry.embedding
        LIMIT :per_entry
    ) AS nearest
    """
).bindparams(bindparam("entry_ids"), bindparam("per_entry"))


async def nearest(known: list[KnownTopic], entry_ids: list[int]) -> list[KnownTopic]:
    """Темы, ближайшие к записям пачки, плюс те, у которых вектора нет вовсе.

    Тема без вектора остаётся в списке всегда. Такие темы существуют: вектор
    считается при заведении, а провайдер эмбеддингов в этот момент мог лежать
    (`service._topic_vectors`). Выкинув их из кандидатов, мы сделали бы
    их недостижимыми навсегда — попасть в них модель больше не смогла бы,
    и на каждой записи заводился бы дубль.
    """
    if not entry_ids:
        return known

    async with session_scope() as session:
        rows = (await session.execute(SQL, {"entry_ids": entry_ids, "per_entry": PER_ENTRY})).all()

    chosen = {int(row[0]) for row in rows}
    return [topic for topic in known if topic.id in chosen or topic.embedding_missing]
