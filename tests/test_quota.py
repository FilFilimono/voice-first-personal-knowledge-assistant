"""Учёт запросов к провайдеру: сколько потрачено и сколько осталось.

**Зачем.** 20 августа 2026 воркер упёрся в бесплатную суточную квоту
эмбеддингов вслепую: узнать, сколько запросов израсходовано, было неоткуда.
`llm_calls` не получал вызовов эмбеддингов вовсе — учёт стоял в двери
`core/llm`, а эмбеддинги ходят своей дверью.

Поэтому здесь два разных утверждения, и оба надо держать:

1. **каждый HTTP-запрос к провайдеру оставляет строку** — включая проверку
   при старте и включая упавший вызов;
2. **окно суток — тихоокеанское**, а не владельца. Проверено по документации
   провайдера: RPD сбрасывается в полночь по Pacific Time. Счёт по полуночи
   в Риме ошибался бы на девять часов и врал бы ровно в тот момент, когда
   квота нужна.

Требует поднятого Postgres: `just up`.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, insert, select

import brain.core.quota as quota
from brain.core import llm
from brain.core.llm.gemini import GeminiChat
from brain.core.ports import Completion, EntryText
from brain.core.retrieval.errors import EmbeddingError
from brain.core.retrieval.gemini import EMBEDDING, GeminiEmbedder
from brain.storage.db import session_scope
from brain.storage.models.chunk import DIMENSIONS
from brain.storage.models.llm_call import LlmCall

pytestmark = pytest.mark.integration

MODEL = "gemini-embedding-001"
Handler = Callable[[httpx.Request], httpx.Response]

# Ответ провайдера, когда суточная квота исчерпана.
OVER_QUOTA: dict[str, Any] = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
    }
}


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


def _embedder(handler: Handler) -> GeminiEmbedder:
    return GeminiEmbedder(
        api_key="test-key",
        model=MODEL,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ok() -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content).get("requests", [1]))
        return httpx.Response(200, json={"embeddings": [{"values": [0.5] * DIMENSIONS}] * count})

    return handler


def _refuses(status: int, payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


async def _rows() -> list[LlmCall]:
    async with session_scope() as session:
        return list((await session.scalars(select(LlmCall).order_by(LlmCall.id))).all())


async def test_embedding_call_is_recorded() -> None:
    """Раньше вызовов эмбеддингов в `llm_calls` не было вовсе.

    Именно поэтому квоту нельзя было посчитать: считать было не из чего.
    """
    await _embedder(_ok()).embed_documents(["раз", "два"])

    [row] = await _rows()
    assert row.tier == EMBEDDING
    assert row.model == MODEL
    assert row.succeeded
    # Батч — **один** HTTP-запрос, и квота считает запросы, а не тексты.
    assert row.entries_count == 2


async def test_one_batch_is_one_request() -> None:
    """Квота провайдера считается в запросах. Строка на запрос, а не на текст."""
    await _embedder(_ok()).embed_documents(["раз", "два", "три", "четыре"])

    assert len(await _rows()) == 1


async def test_startup_check_spends_quota_too() -> None:
    """Проверка при старте — такой же запрос к провайдеру, как остальные.

    Учёт стоит в провайдере, а не в двери `core/retrieval/embeddings`, ровно
    поэтому: `check_available` идёт мимо двери, а для квоты нужен каждый запрос.
    """
    await _embedder(_ok()).check_available()

    [row] = await _rows()
    assert row.purpose == "embed-check"


async def test_refused_call_is_recorded_but_not_counted_as_spent() -> None:
    """Отказ по квоте записывается, но в израсходованные не идёт.

    Считать его потраченным значило бы раздувать счёт ретраями: пять попыток
    на задание превратили бы тысячу запросов в тысячу двести, и число
    перестало бы отвечать на вопрос «сколько из тысячи я использовал».
    """
    with pytest.raises(EmbeddingError):
        await _embedder(_refuses(429, OVER_QUOTA)).embed_documents(["раз"])

    [row] = await _rows()
    assert not row.succeeded
    assert row.error is not None and "429" in row.error

    usage = await quota.used(datetime.now(UTC), tier=EMBEDDING, limit=1000)
    assert usage.spent == 0
    assert usage.refused == 1


async def test_query_and_document_are_told_apart() -> None:
    """Поиск и индексация тратят одну квоту, но разбирать расход надо отдельно."""
    embedder = _embedder(_ok())
    await embedder.embed_documents(["текст записи"])
    await embedder.embed_query("что я думал про поиск")

    purposes = {row.purpose for row in await _rows()}
    assert purposes == {"embed-document", "embed-query"}


async def test_quota_window_is_pacific_not_the_owners_midnight() -> None:
    """Провайдер сбрасывает RPD в полночь по тихоокеанскому времени.

    Проверено по документации, а не по памяти. Счёт по полуночи владельца
    (Europe/Rome) ошибался бы на девять часов — и в эти девять часов показывал
    бы вчерашний расход как сегодняшний.
    """
    # 08:00 UTC 20 августа — это 01:00 в Лос-Анджелесе того же дня
    # и 10:00 в Риме. Тихоокеанская полночь уже прошла, римская — тоже,
    # но границы у них разные.
    morning = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    assert quota.window_start(morning) == datetime(2026, 8, 20, 7, 0, tzinfo=UTC)

    # 03:00 UTC — это ещё 20:00 предыдущего дня в Лос-Анджелесе, но уже
    # 05:00 нового дня в Риме. Окно обязано остаться вчерашним.
    night = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
    assert quota.window_start(night) == datetime(2026, 8, 19, 7, 0, tzinfo=UTC)


async def test_usage_counts_only_this_window() -> None:
    """Вчерашний расход в сегодняшний счёт не идёт."""
    await _embedder(_ok()).embed_documents(["сегодняшний"])

    async with session_scope() as session:
        # Строка из прошлого окна — как если бы её записали позавчера.
        await session.execute(
            insert(LlmCall).values(
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                provider="gemini",
                model=MODEL,
                tier=EMBEDDING,
                purpose="embed-document",
                input_tokens=0,
                output_tokens=0,
                entries_count=1,
                latency_ms=1,
                succeeded=True,
            )
        )
        assert await session.scalar(select(func.count()).select_from(LlmCall)) == 2

    usage = await quota.used(datetime.now(UTC), tier=EMBEDDING, limit=1000)
    assert usage.spent == 1
    assert usage.left == 999


# --- Текстовая модель ---


class SlowRefusal:
    """Модель, которая думает и только потом отказывает.

    Отказ провайдера редко приходит мгновенно: таймаут чтения в `core/llm`
    стоит на 180 секундах, и упавший вызов держит воркер ровно столько.
    Фейк воспроизводит именно это — время, потраченное впустую.
    """

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return "gemini-3.7-flash"

    @property
    def tier(self) -> str:
        return llm.CHEAP

    async def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any], temperature: float
    ) -> Completion:
        await asyncio.sleep(0.05)
        raise llm.LlmError("провайдер не ответил", status=429)


async def test_a_refused_text_call_records_the_time_it_took() -> None:
    """Ноль в `latency_ms` у отказа — порча измерителя, а не косметика.

    `CLEANUP_WAIT_SEC` ставится по неделе живых данных из этой самой колонки.
    Вызов, простоявший до таймаута и упавший, попадал в выборку **самым
    быстрым вызовом дня** и тянул p95 вниз: чем хуже реальность, тем сильнее
    врал прибор.
    """
    with pytest.raises(llm.LlmError):
        await llm.complete_over_entries(
            SlowRefusal(),
            entries=[EntryText(entry_id=1, text="текст записи", is_private=False)],
            system="вычисти",
            schema={},
            temperature=0.0,
            purpose="cleanup",
        )

    [row] = await _rows()
    assert not row.succeeded
    assert row.latency_ms > 0, "упавший вызов записан как мгновенный"


async def test_the_text_model_probe_is_recorded_too() -> None:
    """Проба при старте — такой же запрос против квоты, как остальные.

    Раньше её в `llm_calls` не было вовсе: `check_available` жила в провайдере
    и звала `complete` мимо двери, где ведётся учёт. В разработке воркер
    перезапускается часто, и эти запросы уходили в лимит невидимо — ровно
    та слепота, которую ADR-0014 чинил у эмбеддингов.
    """
    answer = {
        "candidates": [
            {"content": {"parts": [{"text": '{"text": "готово"}'}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer)

    await llm.check_available(
        GeminiChat(
            api_key="test-key",
            model="gemini-3.7-flash",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )

    [row] = await _rows()
    assert row.purpose == llm.CHECK_PURPOSE
    assert row.tier == llm.CHEAP
    assert row.succeeded


async def test_unknown_limit_is_shown_as_unknown_not_as_zero() -> None:
    """Лимит текстовой модели нам неизвестен, и выдумывать его нельзя.

    Число, взятое из головы, в `/status` читалось бы как измеренное — ровно
    та ошибка, из-за которой в проекте цены помечены датой проверки.
    """
    usage = await quota.used(datetime.now(UTC), tier=EMBEDDING, limit=None)

    assert usage.limit is None
    assert usage.left is None
    assert not usage.exhausted
