"""Дешёвый тир: Gemini Flash-Lite одним POST.

SDK провайдера намеренно не берётся — по той же причине, что в `core/stt`
и `core/retrieval`: весь протокол виден в этом файле целиком, а второй
провайдер это такой же файл рядом.

**Структурированный ответ — не просьба, а ограничение на стороне провайдера.**
`responseSchema` заставляет модель вернуть ровно те поля и ровно те значения
enum'а, которые мы назвали; разбирать свободный текст и уговаривать модель
«отвечай только JSON» не приходится. Проверено вызовом 19.08.2026, там же
найдено ограничение: пустая строка внутри `enum` отклоняется с
`400 enum[N]: cannot be empty`, поэтому «значения нет» выражается **отсутствием
поля**, а не пустой строкой.

**Про температуру.** Она приходит снаружи, и у разметки она **не нулевая** —
в отличие от расшифровки, где стоит 0.0. Это не небрежность, а измерительный
прибор: состояние разметки считается по расхождению двух прогонов (ADR-0011),
и без разброса расхождения не бывает. Заодно замер показал, что 0.0 у этого
провайдера всё равно ничего не фиксирует: три прогона при нулевой температуре
разошлись на 17 записях из 29.

**Про уровень рассуждения.** Он необязателен и по умолчанию не шлётся вовсе:
не попросили — тело запроса ровно то же, что было до его появления. Иначе
правка ради одной модели уехала бы в разметку, в которой этот файл участвует
той же дверью, а её замеры сняты на другом теле запроса.

**Проверки при старте здесь нет намеренно.** Она живёт в двери `core/llm`,
потому что там ведётся учёт вызовов, а проба — такой же запрос против квоты,
как остальные. Пока она стояла тут, её вызовов в `llm_calls` не было вовсе.
"""

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from brain.core.llm.errors import LlmError
from brain.core.ports import Completion

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Пачка записей отвечает дольше одной. Ретраями занимается очередь, поэтому
# здесь нужен только потолок, за которым ожидание перестаёт быть осмысленным.
TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

CHEAP = "cheap"


class GeminiChat:
    """Один POST со схемой ответа."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        thinking_level: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # Уровень рассуждения — свойство **модели**, а не вызова: у одной он
        # не поддерживается вовсе, у другой стоит по умолчанию, и вызывающему
        # про это знать нечего. Поэтому он приходит в конструктор один раз,
        # а не тянется параметром через каждый `complete`.
        #
        # `None` означает «не просили» — и тогда тело запроса обязано остаться
        # прежним целиком. Этим же классом ходит разметка, где ненулевая
        # температура работает измерительным прибором (ADR-0011): лишний ключ
        # в её запросе менял бы поведение, о котором сняты замеры.
        self._thinking_level = thinking_level
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
    def tier(self) -> str:
        return CHEAP

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        temperature: float,
    ) -> Completion:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": dict(schema),
            },
        }

        if self._thinking_level is not None:
            # Ключ **вложенный**, и это не стиль, а протокол. Проверено живым
            # вызовом 25 августа 2026: плоское `generationConfig.thinkingLevel`
            # провайдер отвергает с `400 Unknown name "thinkingLevel"
            # at 'generation_config': Cannot find field`.
            #
            # Значение не проверяется здесь: словарь уровней принадлежит
            # провайдеру и у разных моделей разный (`gemini-3.7-flash`
            # отвечает `400 Thinking level MINIMAL is not supported for this
            # model`, а у `gemini-3.5-flash-lite` `minimal` стоит умолчанием).
            # Список, замороженный в нашем коде, устарел бы молча — вместо него
            # неверный уровень ловит проба при старте, одной строкой в логе.
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self._thinking_level}

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
                # Имя модели — в тексте любой ошибки: настройка живёт в `.env`,
                # и первый вопрос по логу всегда «а какая модель-то была».
                # Код едет объектом, а не только в тексте: по тексту старт
                # не отличал кончившуюся квоту от снятой модели.
                raise LlmError(
                    f"{self.provider}/{self._model}: HTTP {error.response.status_code}. "
                    f"{_error_message(error.response)}",
                    status=error.response.status_code,
                ) from error
            except (httpx.HTTPError, ValueError) as error:
                raise LlmError(f"{self.provider}/{self._model} не ответил: {error}") from error

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = payload.get("usageMetadata") or {}
        return Completion(
            data=self._extract(payload),
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
            latency_ms=latency_ms,
        )

    def _extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Достать разобранный ответ, не принимая пустоту за успех.

        Обрыв по лимиту токенов выглядит как валидный ответ с оборванным JSON —
        и это не выдумка: пачка из полусотни записей упирается в потолок вывода
        легко. Молча разобрав такой ответ наполовину, мы потеряли бы хвост пачки
        и не узнали бы об этом.
        """
        try:
            candidate = payload["candidates"][0]
            parts = candidate["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as error:
            raise LlmError(
                f"{self.provider}/{self._model}: ответ не разобран: {str(payload)[:300]}"
            ) from error

        reason = candidate.get("finishReason")
        if reason not in (None, "STOP"):
            raise LlmError(
                f"{self.provider}/{self._model}: ответ оборван ({reason}). "
                "Пачка слишком большая или упёрлись в потолок вывода."
            )

        try:
            parsed = json.loads(text)
        except ValueError as error:
            raise LlmError(f"{self.provider}/{self._model}: ответ не JSON: {text[:300]}") from error

        if not isinstance(parsed, dict):
            raise LlmError(f"{self.provider}/{self._model}: ждали объект, пришло {type(parsed)}")
        return parsed


def _error_message(response: httpx.Response) -> str:
    """Объяснение провайдера, если оно есть. Оно обычно и есть ответ."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:300]
    return str(message)
