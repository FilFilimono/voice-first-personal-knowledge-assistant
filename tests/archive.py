"""Живой архив для тестов — из базы, а не из репозитория.

**Зачем так.** Два правила проекта тянут в разные стороны.

Первое, из `status.md` после дыр 16 и 18 августа: *данные для тестов берутся
из базы, а не из головы*. Придуманная «похожая» строка не ломает код так,
как ломает живая речь: «напомнил» вместо «напомни», «чегнуть» вместо
«чекнуть», «порасгоняли» вместо «поразгоняли».

Второе — приватность ([ADR-0002](../docs/adr/0002-privacy.md)): архив наружу
не уходит, и git это тоже наружу. Опубликованную историю не отозвать.

Оба выполняются так: тексты берутся из живой базы при прогоне и складываются
в кэш рядом, а кэш в git не попадает. Нет базы и нет кэша — тест **скипается
с объяснением**, а не выдумывает данные и не притворяется зелёным.

**Цена названа вслух.** На чистой машине и в CI тесты на живой речи скипаются.
Пропущенный тест зелёным не считается, и заметить это можно только по счётчику
`skipped` в конце прогона. Поэтому сообщение о пропуске говорит, что делать.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings

DATA = Path(__file__).parent / "data"
CACHE = DATA / "archive_cache.json"

# Адрес рабочей базы снимается на импорте — до того, как фикстура
# `migrated_database` подменит `DATABASE_URL` на одноразовую базу.
# Модули тестов импортируются на сборе, фикстуры выполняются после,
# поэтому здесь всегда настоящий архив, а не пустая одноразовая схема.
WORKING_DATABASE_URL = get_settings().database_url


@dataclass(frozen=True, slots=True)
class Entry:
    """Запись архива в том виде, в каком её видят тесты."""

    id: int
    kind: str
    created_at: str
    timezone: str
    is_private: bool
    text: str


SQL = """
    SELECT e.id, e.kind::text, e.created_at, e.timezone, e.is_private,
           coalesce(t.text, e.raw_text) AS text
    FROM entries e
    LEFT JOIN LATERAL (
        SELECT text FROM transcripts WHERE source_entry_id = e.id ORDER BY id DESC LIMIT 1
    ) t ON true
    WHERE coalesce(t.text, e.raw_text) IS NOT NULL
    ORDER BY e.id
"""


async def _read_database() -> list[dict[str, Any]] | None:
    """Прочитать архив. `None`, если базы нет — это не ошибка, а причина скипа."""
    engine = create_async_engine(WORKING_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(SQL))).all()
    except Exception:
        return None
    finally:
        await engine.dispose()

    return [
        {
            "id": int(row[0]),
            "kind": str(row[1]),
            "created_at": row[2].isoformat(),
            "timezone": str(row[3]),
            "is_private": bool(row[4]),
            "text": str(row[5]),
        }
        for row in rows
    ]


def rebuild() -> list[dict[str, Any]] | None:
    """Перечитать архив из базы и переписать кэш.

    Синхронная нарочно: зовётся из сессионной фикстуры и из `__main__`,
    то есть снаружи тестового event loop.
    """
    entries = asyncio.run(_read_database())
    if entries is None:
        return None

    DATA.mkdir(exist_ok=True)
    CACHE.write_text(
        json.dumps(
            {
                "откуда": "живая база, собрано прогоном тестов",
                "внимание": "в git не попадает — см. tests/data/README.md и ADR-0002",
                "записи": entries,
            },
            ensure_ascii=False,
        )
    )
    return entries


def load() -> list[Entry] | None:
    """Архив: из базы, если она есть, иначе из кэша. `None` — нет ни того, ни другого."""
    entries = rebuild()

    if entries is None and CACHE.exists():
        entries = json.loads(CACHE.read_text())["записи"]

    if entries is None:
        return None
    return [Entry(**row) for row in entries]


# --- Вектора ---
#
# Считаются настоящей моделью один раз и кладутся рядом с текстами, в тот же
# игнорируемый git'ом каталог: ключом здесь служит сам текст записи, так что
# файл с векторами — это тот же архив, только в другом виде.
#
# Заглушка вместо настоящих векторов тут не годится: «находит по смыслу,
# когда общих слов нет» на хэше от текста зелёный при любом поиске.

VECTORS = DATA / "archive_vectors.json"


def _cached_vectors() -> dict[str, dict[str, list[float]]]:
    if not VECTORS.exists():
        return {"документы": {}, "запросы": {}}
    loaded: dict[str, dict[str, list[float]]] = json.loads(VECTORS.read_text())
    return loaded


async def _embed(texts: list[str], *, kind: str) -> list[list[float]]:
    from brain.core.retrieval.gemini import GeminiEmbedder

    settings = get_settings()
    embedder = GeminiEmbedder(
        api_key=settings.google_api_key,
        model=settings.embedding_model,
    )
    if kind == "документы":
        return await embedder.embed_documents(texts)
    return [await embedder.embed_query(text_) for text_ in texts]


async def vectors(
    *, documents: list[str], queries: list[str]
) -> dict[str, dict[str, list[float]]] | None:
    """Вектора для текстов и запросов. `None`, если считать нечем и кэша нет.

    Досчитывается только недостающее: поменялось правило резки — новые чанки
    посчитаются, старые останутся лежать. Прогон стоит доли цента.

    Асинхронная, потому что зовётся из тестов — а они уже внутри event loop,
    и `asyncio.run` там невозможен.
    """
    cache = _cached_vectors()
    missing = {
        "документы": [text_ for text_ in documents if text_ not in cache["документы"]],
        "запросы": [text_ for text_ in queries if text_ not in cache["запросы"]],
    }

    if any(missing.values()):
        if not get_settings().google_api_key.strip():
            return None
        for kind, texts in missing.items():
            if texts:
                cache[kind].update(zip(texts, await _embed(texts, kind=kind), strict=True))
        DATA.mkdir(exist_ok=True)
        VECTORS.write_text(json.dumps(cache, ensure_ascii=False))

    return cache


NO_VECTORS = (
    "нечем посчитать вектора: в кэше их нет, а GOOGLE_API_KEY не задан.\n"
    "Вектора считаются настоящей моделью один раз и в git не попадают "
    "(ADR-0002: архив в репозиторий не уходит, а ключ кэша — это текст записи)."
)

NO_ARCHIVE = (
    "нет живого архива: база недоступна и кэша нет.\n"
    "Тесты на живой речи проверяют то, что нельзя выдумать, поэтому данные "
    "берутся из базы, а не из репозитория (ADR-0002: архив в git не попадает).\n"
    "Поднять базу: just up && just migrate"
)


def entry(entries: list[Entry], entry_id: int) -> Entry:
    """Запись по номеру. Нет такой — тест обязан сказать об этом внятно."""
    for item in entries:
        if item.id == entry_id:
            return item
    raise AssertionError(
        f"в архиве нет записи {entry_id}. Тесты на живой речи привязаны "
        "к номерам записей — если архив другой, их надо перепривязать, "
        "а не подставлять похожий текст."
    )


if __name__ == "__main__":  # pragma: no cover — разовая команда
    result = rebuild()
    if result is None:
        raise SystemExit(f"\n{NO_ARCHIVE}\n")
    print(f"Записей в кэше: {len(result)} → {CACHE}")
