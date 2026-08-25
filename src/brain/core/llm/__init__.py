"""Дверь наружу для текста записей, уходящего в модель.

Здесь три вещи, и все три стоят именно здесь, а не в вызывающем коде.

**Инвариант 5.** Приватная запись наружу не уходит. Вызывающий не обязан
помнить о приватности и не должен уметь её обойти — так же устроены `core/stt`
и `core/retrieval`, и по той же причине.

**Инвариант 2 и маршрутизация.** Тир выбирается здесь, а не у вызывающего:
он просит `cheap` или `expensive` и назначение вызова, а не модель (ADR-0004).
Обращение к `expensive` до Фазы 6 падает явно и не деградирует молча
в дешёвую модель.

**Учёт стоимости и запросов.** Строка в `llm_calls` пишется в той же функции,
что делает вызов (`_call`), поэтому забыть её невозможно. Через неё же идёт
**проверка модели при старте**: раньше она жила в провайдере, звала `complete`
напрямую и не оставляла в `llm_calls` ни строки — при том, что квота
у провайдера считается в запросах (ADR-0014). Время вызова меряется тоже
здесь: у упавшего вызова провайдер его не сообщает, а ноль в колонке ломает
любой расчёт по ней.

**Почему дверь принимает записи, а не готовый текст промпта.** Если сюда
передавать собранную строку, проверить в ней нечего: приватный текст уже внутри
и отличить его не от чего. Поэтому конверт пачки собирается здесь, а снаружи
приходит только то, что у пачки спрашивают, — инструкция и схема ответа.
Заодно этот конверт один на всех: разметку, вечерний разбор и поиск
противоречий устраивает один и тот же формат `[id] текст`.
"""

import logging
import time
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

# Назначение пробы при старте. Отдельное, как `embed-check` у эмбеддингов:
# расход надо уметь разобрать, а в разработке воркер перезапускается часто,
# и проб за день набирается больше, чем разметок.
CHECK_PURPOSE = "chat-check"

# Чем проверяется модель при старте. Схема **настоящая** — объект со строковым
# полем, ровно как у чистки. Прежняя проба слала одно булево поле, то есть
# ловила «модели нет» и не ловила «модель есть и бесполезна»: 25 августа 2026
# воркер написал «модель чистки доступна» про модель, которая на рабочем
# запросе не отвечает.
#
# Текст при этом **минимальный**, а не сопоставимый с рабочим. Проверяется,
# что модель умеет ответить по схеме, а не то, как быстро она это делает:
# скорость меряется замером на архиве, а не одним вызовом при каждом старте,
# за который платит квота.
CHECK_SYSTEM = "Отвечай строго по схеме."
CHECK_PROMPT = "Ответь словом «готово»."
CHECK_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def build_model(
    settings: Settings,
    *,
    tier: str = CHEAP,
    model: str | None = None,
    thinking_level: str | None = None,
) -> LanguageModel:
    """Модель по тиру и настройке.

    Дорогой тир не зарегистрирован до Фазы 6 — там же заводится ключ Anthropic.
    Падать он обязан здесь и с объяснением: молчаливая подмена тира выглядит
    как «стало хуже отвечать», и ищут её неделю (ADR-0004).

    `model` перебивает имя из настроек. Нужен ровно одному месту — прогону
    eval'а, где две модели сравниваются на одном наборе. Настройки заморожены
    (`frozen=True`) намеренно, поэтому подмена делается здесь, а не в них.

    `thinking_level` необязателен и по умолчанию не задан. «Не задан» означает
    не «уровень по умолчанию», а «мы про уровень не говорим вовсе»: тело
    запроса тогда байт в байт прежнее, и разметка со снятыми на ней замерами
    не меняется от того, что чистке понадобилась настройка.
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
                thinking_level=thinking_level,
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

    return await _call(
        model,
        system=system,
        prompt=prompt,
        schema=schema,
        temperature=temperature,
        purpose=purpose,
        entries_count=len(entries),
    )


async def check_available(model: LanguageModel) -> None:
    """Проверить модель настоящим вызовом при старте воркера.

    **Почему проверка живёт в двери, а не в провайдере.** Учёт вызовов
    текстовой модели ведётся здесь и только здесь — этим дверь и ценна: забыть
    строку в `llm_calls` невозможно, потому что мимо неё не пройти. Пока проба
    стояла в `GeminiChat.check_available` и звала `complete` напрямую, она шла
    мимо учёта, и её запросов в `llm_calls` не было **вовсе** — при том, что
    квота у провайдера считается в запросах (ADR-0014), а в разработке воркер
    перезапускается по десятку раз в день.

    Чинить это копией учёта внутри каждого провайдера значило бы завести второе
    место, где о нём надо помнить, — и второй провайдер про него бы не знал.
    У эмбеддингов выбор обратный и по обратной причине: там `check_available`
    физически идёт мимо двери, и единственное общее место — сам провайдер.

    Проба заодно проверяет структурированный ответ: модель может отвечать
    на `generateContent` и не уметь `responseSchema`, и выяснять это на первой
    же пачке было бы поздно.
    """
    try:
        await _call(
            model,
            system=CHECK_SYSTEM,
            prompt=CHECK_PROMPT,
            schema=CHECK_SCHEMA,
            temperature=0.0,
            purpose=CHECK_PURPOSE,
            entries_count=0,
        )
    except LlmError as error:
        # Код ответа переносится, а не теряется при заворачивании: по нему
        # старт решает, вставать громко или работать дальше (ADR-0014).
        raise ProviderUnavailableError(
            f"модель {model.model} недоступна: {error}", status=error.status
        ) from error


async def _call(
    model: LanguageModel,
    *,
    system: str,
    prompt: str,
    schema: Mapping[str, Any],
    temperature: float,
    purpose: str,
    entries_count: int,
) -> Completion:
    """Вызвать модель и записать вызов. Единственное место учёта текстовых моделей.

    **Время меряется здесь, а не берётся из ответа провайдера.** У успешного
    вызова ответ его несёт, у упавшего — нет, и в `llm_calls` уезжал ноль.
    Это порча измерителя: `CLEANUP_WAIT_SEC` ставится по живым данным из этой
    колонки, и вызов, простоявший до таймаута в 180 секунд и упавший, попадал
    в выборку самым быстрым вызовом дня и тянул p95 вниз — прибор врал тем
    сильнее, чем хуже была реальность.

    Одна мерка на обе ветки и на всех провайдеров: колонка обещает «сколько
    занял вызов», и заполняемая двумя разными способами она обещала бы
    «сколько занял вызов, если он удался».
    """
    started = time.monotonic()
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
        #
        # Отсюда `status_code` попадает в базу для текстовой модели: признаком
        # придерживания косметики служит **наблюдённый** отказ, а наблюдается
        # он здесь (`core/present/pacing.py`).
        await record(
            provider=model.provider,
            model=model.model,
            tier=model.tier,
            purpose=purpose,
            entries_count=entries_count,
            latency_ms=_elapsed_ms(started),
            succeeded=False,
            error=str(error)[:500],
            status_code=error.status,
        )
        raise

    latency_ms = _elapsed_ms(started)
    cost = await record(
        provider=model.provider,
        model=model.model,
        tier=model.tier,
        purpose=purpose,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        entries_count=entries_count,
        latency_ms=latency_ms,
    )
    logger.info(
        "%s: записей %s, токенов %s+%s, %s мс, $%s",
        purpose,
        entries_count,
        completion.input_tokens,
        completion.output_tokens,
        latency_ms,
        "?" if cost is None else f"{cost:.6f}",
    )
    return completion


def _elapsed_ms(started: float) -> int:
    """Сколько прошло с начала вызова. `monotonic`, а не часы: их переводят."""
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "CHEAP",
    "CHECK_PURPOSE",
    "EXPENSIVE",
    "Completion",
    "EntryText",
    "LanguageModel",
    "LlmError",
    "PrivateContentError",
    "ProviderUnavailableError",
    "TierNotAvailableError",
    "build_model",
    "check_available",
    "complete_over_entries",
]
