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
from collections.abc import Callable
from typing import Any

import httpx

from brain.core.retrieval.errors import EmbeddingError, ProviderUnavailableError
from brain.storage.models.chunk import DIMENSIONS

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

# Асимметрия не косметическая: модель обучена так, что вопрос и текст архива
# кладутся в пространство по-разному, и одинаковый taskType портит выдачу.
DOCUMENT = "RETRIEVAL_DOCUMENT"
QUERY = "RETRIEVAL_QUERY"

TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


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
            await self._embed(["ok"], DOCUMENT)
        except EmbeddingError as error:
            raise ProviderUnavailableError(
                f"модель эмбеддингов {self._model} недоступна: {error}"
            ) from error

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Посчитать вектора кусков записи. Пустой список — без вызова."""
        if not texts:
            return []
        return await self._embed(texts, DOCUMENT)

    async def embed_query(self, text: str) -> list[float]:
        """Посчитать вектор поискового запроса."""
        [vector] = await self._embed([text], QUERY)
        return vector

    async def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
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
                raise EmbeddingError(
                    f"{self.provider}/{self._model}: HTTP {error.response.status_code}. "
                    f"{_error_message(error.response)}"
                ) from error
            except (httpx.HTTPError, ValueError) as error:
                raise EmbeddingError(
                    f"{self.provider}/{self._model} не ответил: {error}"
                ) from error

        return [_unit(values, self._model) for values in self._extract(payload, len(texts))]

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
