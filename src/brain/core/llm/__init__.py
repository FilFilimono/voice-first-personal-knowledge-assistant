"""Дверь наружу для текста записей, уходящего в модель.

Здесь три вещи, и все три стоят именно здесь, а не в вызывающем коде.

**Инвариант 5.** Приватная запись наружу не уходит. Вызывающий не обязан
помнить о приватности и не должен уметь её обойти — так же устроены `core/stt`
и `core/retrieval`, и по той же причине.

**Инвариант 2 и маршрутизация.** Тир выбирается здесь, а не у вызывающего:
он просит `cheap` или `expensive` и назначение вызова, а не модель (ADR-0004).
Обращение к `expensive` до Фазы 6 падает явно и не деградирует молча
в дешёвую модель.

**Учёт стоимости.** Строка в `llm_calls` пишется в той же функции, что делает
вызов, поэтому забыть её невозможно.

**Почему дверь принимает записи, а не готовый текст промпта.** Если сюда
передавать собранную строку, проверить в ней нечего: приватный текст уже внутри
и отличить его не от чего. Поэтому конверт пачки собирается здесь, а снаружи
приходит только то, что у пачки спрашивают, — инструкция и схема ответа.
Заодно этот конверт один на всех: разметку, вечерний разбор и поиск
противоречий устраивает один и тот же формат `[id] текст`.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from brain.config import Settings
from brain.core.llm.accounting import record
from brain.core.llm.errors import (
    LlmError,
    PrivateContentError,
    ProviderUnavailableError,
    TierNotAvailableError,
)
from brain.core.llm.gemini import CHEAP, GeminiChat
from brain.core.ports import Completion, EntryText, LanguageModel

logger = logging.getLogger(__name__)

EXPENSIVE = "expensive"


def build_model(
    settings: Settings, *, tier: str = CHEAP, model: str | None = None
) -> LanguageModel:
    """Модель по тиру и настройке.

    Дорогой тир не зарегистрирован до Фазы 6 — там же заводится ключ Anthropic.
    Падать он обязан здесь и с объяснением: молчаливая подмена тира выглядит
    как «стало хуже отвечать», и ищут её неделю (ADR-0004).

    `model` перебивает имя из настроек. Нужен ровно одному месту — прогону
    eval'а, где две модели сравниваются на одном наборе. Настройки заморожены
    (`frozen=True`) намеренно, поэтому подмена делается здесь, а не в них.
    """
    if tier == EXPENSIVE:
        raise TierNotAvailableError(
            "дорогой тир подключается в Фазе 6 (ADR-0004): ключа Anthropic "
            "в проекте ещё нет, и подменять его дешёвой моделью нельзя"
        )

    match settings.enrich_provider:
        case "gemini":
            return GeminiChat(
                api_key=settings.google_api_key,
                model=model or settings.enrich_model,
            )


async def complete_over_entries(
    model: LanguageModel,
    *,
    entries: Sequence[EntryText],
    system: str,
    schema: Mapping[str, Any],
    temperature: float,
    purpose: str,
) -> Completion:
    """Спросить модель про пачку записей. Единственная дверь наружу для текста.

    Приватная запись поднимает исключение, а не отфильтровывается тихо. Тихий
    пропуск означал бы, что пачка молча уехала неполной и разметки у записи
    не будет никогда — а узнать об этом было бы неоткуда. Отбирать записи для
    пачки обязан вызывающий, и он это делает запросом к базе, где приватность
    и так есть.
    """
    private = [entry.entry_id for entry in entries if entry.is_private]
    if private:
        raise PrivateContentError(
            f"приватные записи не уходят в модель (инвариант 5): {private}. "
            "Их разметка не делается вовсе — так решено в ADR-0002"
        )

    if not entries:
        # Пустая пачка — законное состояние (все записи уже размечены),
        # но платить за вызов ни о чём не за что.
        return Completion(data={}, input_tokens=0, output_tokens=0, latency_ms=0)

    prompt = "\n\n".join(f"[{entry.entry_id}] {entry.text}" for entry in entries)

    try:
        completion = await model.complete(
            system=system,
            prompt=prompt,
            schema=schema,
            temperature=temperature,
        )
    except LlmError as error:
        # Провайдер берёт деньги за попытку, поэтому упавший вызов учитывается
        # тоже. Иначе месяц ретраев не виден в расходах вовсе.
        await record(
            provider=model.provider,
            model=model.model,
            tier=model.tier,
            purpose=purpose,
            entries_count=len(entries),
            succeeded=False,
            error=str(error)[:500],
        )
        raise

    cost = await record(
        provider=model.provider,
        model=model.model,
        tier=model.tier,
        purpose=purpose,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        entries_count=len(entries),
        latency_ms=completion.latency_ms,
    )
    logger.info(
        "%s: записей %s, токенов %s+%s, %s мс, $%s",
        purpose,
        len(entries),
        completion.input_tokens,
        completion.output_tokens,
        completion.latency_ms,
        "?" if cost is None else f"{cost:.6f}",
    )
    return completion


__all__ = [
    "CHEAP",
    "EXPENSIVE",
    "Completion",
    "EntryText",
    "LanguageModel",
    "LlmError",
    "PrivateContentError",
    "ProviderUnavailableError",
    "TierNotAvailableError",
    "build_model",
    "complete_over_entries",
]
