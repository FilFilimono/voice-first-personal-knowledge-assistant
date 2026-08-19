"""Эмбеддинги через Gemini Embedding.

Тот же ключ Google, что у расшифровки и дешёвого тира — на один сервис
и один счёт меньше (ADR-0006). SDK провайдера намеренно не берётся: запрос —
один POST, и весь протокол виден в этом файле целиком, как в `core/stt/gemini.py`.

**Нормировка — главное, что здесь происходит.** Проверено вызовом 18 августа
2026: `gemini-embedding-001` отдаёт срез MRL до 768 с нормой ≈0.583, до 1536 —
≈0.690, и только полные 3072 приходят единичными. Срез при этом честный —
это ровно префикс полного вектора, — но длина у него своя.

Косинусу это безразлично: `cos(a,b)` делит на нормы, и умножение вектора
на 0.583 его не меняет. Ломается всё остальное, и молча:

- `<#>` (inner product) и `<->` (L2) дают другой порядок. На единичных векторах
  inner product равен косинусу и считается дешевле — первый, кто возьмёт
  быстрый оператор, получит тихую деградацию выдачи;
- порог косинуса 0.75 для связей (ADR-0006, инвариант 8) — это сравнение
  с константой, и на векторах чужой длины константа значит не то, что написано;
- `gemini-embedding-2` нормирован на всех размерностях. В день сверки моделей
  в одной колонке окажутся вектора двух норм.

Поэтому наружу отсюда уходит только единичный вектор, и это под тестом.
"""

import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from brain.core.llm.accounting import record
from brain.core.retrieval.errors import EmbeddingError, ProviderUnavailableError
from brain.storage.models.chunk import DIMENSIONS

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

# Асимметрия не косметическая: модель обучена так, что вопрос и текст архива
# кладутся в пространство по-разному, и одинаковый taskType портит выдачу.
DOCUMENT = "RETRIEVAL_DOCUMENT"
QUERY = "RETRIEVAL_QUERY"

TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

# Тир для `llm_calls`. Эмбеддинги — не дешёвый и не дорогой тир текстовой
# модели, у них своя квота и свой прайс, и смешивать их в отчёте нельзя.
EMBEDDING = "embedding"

# Назначения. Их три, потому что расход надо уметь разобрать: индексация
# архива, поиск и проверка при старте тратят одну квоту, но по разным поводам.
DOCUMENT_PURPOSE = "embed-document"
QUERY_PURPOSE = "embed-query"
CHECK_PURPOSE = "embed-check"


class GeminiEmbedder:
    """Батч текстов — один POST."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # Шов для тестов: разбор ответа провайдера — самая ошибкоопасная часть
        # файла, и проверять её надо на настоящем коде, но без сети.
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT)

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return DIMENSIONS

    async def check_available(self) -> None:
        """Дёрнуть модель настоящим вызовом при старте воркера.

        Списку моделей верить нельзя — 16 августа 2026 он показывал снятую
        с обслуживания модель как рабочую, а 404 приходил только на вызов.
        """
        try:
            await self._embed(["ok"], DOCUMENT, purpose=CHECK_PURPOSE)
        except EmbeddingError as error:
            # Код переносится, а не теряется при заворачивании: решение
            # «останавливаться или работать дальше» принимается по нему.
            raise ProviderUnavailableError(
                f"модель эмбеддингов {self._model} недоступна: {error}",
                status=error.status,
            ) from error

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Посчитать вектора кусков записи. Пустой список — без вызова."""
        if not texts:
            return []
        return await self._embed(texts, DOCUMENT, purpose=DOCUMENT_PURPOSE)

    async def embed_query(self, text: str) -> list[float]:
        """Посчитать вектор поискового запроса."""
        [vector] = await self._embed([text], QUERY, purpose=QUERY_PURPOSE)
        return vector

    async def _embed(self, texts: list[str], task_type: str, *, purpose: str) -> list[list[float]]:
        body: dict[str, Any] = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    # Умолчание провайдера — 3072, а в колонке 768. Молчание
                    # здесь стоило бы ошибки на первой же вставке в проде.
                    "outputDimensionality": DIMENSIONS,
                }
                for text in texts
            ]
        }

        started = time.monotonic()
        async with self._client_factory() as client:
            try:
                response = await client.post(
                    ENDPOINT.format(model=self._model),
                    # Ключ заголовком, а не в URL: адреса попадают в логи.
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as error:
                # Код ответа едет **объектом**, а не только в тексте: по тексту
                # старт воркера не мог отличить кончившуюся квоту от снятой
                # модели и останавливался из-за временного отказа.
                refused = EmbeddingError(
                    f"{self.provider}/{self._model}: HTTP {error.response.status_code}. "
                    f"{_error_message(error.response)}",
                    status=error.response.status_code,
                )
                await self._count(purpose, len(texts), started, refused)
                raise refused from error
            except (httpx.HTTPError, ValueError) as error:
                silent = EmbeddingError(f"{self.provider}/{self._model} не ответил: {error}")
                await self._count(purpose, len(texts), started, silent)
                raise silent from error

        await self._count(purpose, len(texts), started, None)
        return [_unit(values, self._model) for values in self._extract(payload, len(texts))]

    async def _count(
        self, purpose: str, texts: int, started: float, refusal: EmbeddingError | None
    ) -> None:
        """Записать запрос в `llm_calls`. Одна строка — один HTTP-запрос.

        Учёт стоит **в провайдере**, а не в двери `core/retrieval/embeddings`,
        и это отличается от текстовой модели намеренно. Там дверь — единственный
        путь наружу, и учёт в ней забыть невозможно. Здесь мимо двери ходит
        `check_available`, а для квоты нужен каждый запрос без исключений:
        20 августа 2026 воркер упёрся в суточный лимит вслепую именно потому,
        что вызовов эмбеддингов в `llm_calls` не было вовсе.

        Токены не пишутся: `batchEmbedContents` их не возвращает. Ноль здесь
        честнее выдумки, а стоимость поэтому остаётся неизвестной — см.
        `llm/pricing.py`. Квота считается **в запросах**, не в токенах.
        """
        await record(
            provider=self.provider,
            model=self._model,
            tier=EMBEDDING,
            purpose=purpose,
            # Сколько текстов уехало одним запросом. Для квоты это не важно,
            # для понимания расхода — да: батч из сорока и батч из одного
            # стоят одинаково, и это стоит видеть.
            entries_count=texts,
            latency_ms=int((time.monotonic() - started) * 1000),
            succeeded=refusal is None,
            error=None if refusal is None else str(refusal)[:500],
        )

    def _extract(self, payload: dict[str, Any], expected: int) -> list[list[float]]:
        """Достать вектора, не принимая неполный ответ за успех.

        Порядок провайдер сохраняет — проверено вызовом, — и это критично:
        вектор чанка 3, доставшийся чанку 1, врал бы в выдаче незаметно.
        """
        try:
            vectors = [item["values"] for item in payload["embeddings"]]
        except (KeyError, TypeError) as error:
            raise EmbeddingError(
                f"{self.provider}/{self._model}: ответ не разобран: {str(payload)[:300]}"
            ) from error

        if len(vectors) != expected:
            raise EmbeddingError(
                f"{self.provider}/{self._model}: запрошено {expected} векторов, "
                f"получено {len(vectors)}"
            )
        return vectors


def _unit(values: list[float], model: str) -> list[float]:
    """Привести вектор к единичной длине. Единственная дверь наружу."""
    if len(values) != DIMENSIONS:
        raise EmbeddingError(
            f"{model}: вектор длиной {len(values)}, а в колонке {DIMENSIONS} — "
            "проверьте outputDimensionality и размерность в схеме"
        )

    norm = math.sqrt(sum(x * x for x in values))
    if norm == 0.0:
        # Нулевой вектор не нормируется, и означает он не «текст без смысла»,
        # а то, что считать было нечего. Молча сохранив его, мы получили бы
        # чанк, равноудалённый от всего.
        raise EmbeddingError(f"{model}: нулевой вектор")

    return [x / norm for x in values]


def _error_message(response: httpx.Response) -> str:
    """Объяснение провайдера, если оно есть. Оно обычно и есть ответ."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]
    return str(message)
