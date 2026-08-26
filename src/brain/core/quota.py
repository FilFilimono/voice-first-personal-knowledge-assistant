"""Сколько запросов к провайдеру потрачено за сутки и сколько осталось.

**Зачем.** 20 августа 2026 воркер упёрся в бесплатную суточную квоту
эмбеддингов вслепую: узнать расход было неоткуда. Причина была структурной —
`llm_calls` не получал вызовов эмбеддингов вовсе, учёт стоял только в двери
`core/llm`, а эмбеддинги ходят своей. Теперь строку пишет сам провайдер,
и здесь она считается.

**Окно суток — тихоокеанское, и это проверено, а не вспомнено.** Провайдер
сбрасывает суточный лимит запросов в полночь по Pacific Time
(https://ai.google.dev/gemini-api/docs/rate-limits). Считать по полуночи
владельца было бы естественно и неверно: Europe/Rome опережает Лос-Анджелес
на девять часов, и все девять счётчик показывал бы вчерашний расход как
сегодняшний — ровно в тот момент, когда он нужен.

**Считаются запросы, а не токены и не деньги.** Лимит бесплатного тира
выражен в запросах в сутки, и батч из сорока текстов стоит ровно один запрос.

**Отказ по квоте в расход не идёт.** Иначе пять попыток на задание превратили
бы тысячу запросов в тысячу двести, и число перестало бы отвечать на вопрос
«сколько из тысячи я использовал». Отказы считаются отдельно — по ним видно,
что во что-то упёрлись.

**Считается по моделям, а не на проект целиком** (с 26.08.2026). Лимит
провайдер держит на модель — он сказал это дословно, метрикой
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` в теле `429`, — и общая
сумма «потрачено 58» не отвечает на вопрос, ради которого её смотрят: можно ли
сейчас запустить бэкфилл. У `3.6-flash` двадцать запросов в сутки кончаются
к обеду, а `3.5-flash-lite` за те же сутки делает вдвое больше и не отказывает.
Сами лимиты живут в `core/llm/limits`, рядом с ценами.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from brain.core.llm.limits import daily_limit
from brain.storage.db import session_scope
from brain.storage.models.llm_call import LlmCall

# Где у провайдера кончаются сутки. Не там, где у владельца.
PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class Usage:
    """Расход по одному тиру за текущие сутки провайдера."""

    tier: str
    spent: int
    refused: int
    # `None` — лимит неизвестен. Именно `None`, а не выдуманное число: цифра
    # из головы в `/status` читалась бы как измеренная.
    limit: int | None

    @property
    def left(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.left == 0


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Расход по одной модели за текущие сутки провайдера.

    Отдельный тип, а не `Usage` с другим полем: тир и модель — разные вопросы.
    Тиром спрашивают «во сколько обойдётся команда» (там модель ещё не выбрана),
    моделью — «упрусь ли я сейчас в лимит», и лимит есть только у второго.
    """

    model: str
    spent: int
    refused: int
    # `None` — лимит неизвестен. Именно `None`, а не выдуманное число.
    limit: int | None

    @property
    def left(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.left == 0


def window_start(now: datetime) -> datetime:
    """Начало текущих суток провайдера — последняя полночь по Pacific Time."""
    local = now.astimezone(PACIFIC)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(now.tzinfo or PACIFIC)


def resets_at(now: datetime) -> datetime:
    """Когда квота сбросится. Нужно `/status`: «ждать до» полезнее, чем «кончилась»."""
    return window_start(now) + timedelta(days=1)


async def used(now: datetime, *, tier: str, limit: int | None) -> Usage:
    """Расход по тиру за текущие сутки провайдера."""
    since = window_start(now)

    async with session_scope() as session:
        counted = (
            await session.execute(
                select(
                    func.count().filter(LlmCall.succeeded.is_(True)),
                    func.count().filter(LlmCall.succeeded.is_(False)),
                )
                .select_from(LlmCall)
                .where(LlmCall.tier == tier, LlmCall.created_at >= since)
            )
        ).one()

    return Usage(tier=tier, spent=int(counted[0]), refused=int(counted[1]), limit=limit)


async def by_model(
    now: datetime, *, overrides: Mapping[str, int] | None = None
) -> list[ModelUsage]:
    """Расход за текущие сутки провайдера, разбивкой по моделям.

    Отдаются **все модели, которые сегодня о себе заявили**, включая те, у кого
    были одни отказы: модель, отказавшая двадцать раз подряд, — это ровно то,
    что владелец должен увидеть в `/status`.

    Порядок — **сверху то, во что упёрлись**: модель, получившая отказ или
    выбравшая известный лимит, идёт первой, даже если другая потратила больше.
    Иначе первой строкой шла бы модель, про которую решать нечего: 26.08.2026
    `3.5-flash-lite` сделала 22 запроса и чувствовала себя прекрасно, а вставшая
    на двадцати `3.6-flash` оказалась бы под ней.
    """
    since = window_start(now)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    LlmCall.model,
                    func.count().filter(LlmCall.succeeded.is_(True)),
                    func.count().filter(LlmCall.succeeded.is_(False)),
                )
                .where(LlmCall.created_at >= since)
                .group_by(LlmCall.model)
            )
        ).all()

    usages = [
        ModelUsage(
            model=model,
            spent=int(spent),
            refused=int(refused),
            limit=daily_limit(model, override=(overrides or {}).get(model)),
        )
        for model, spent, refused in rows
    ]
    return sorted(usages, key=_urgency)


def _urgency(usage: ModelUsage) -> tuple[int, int, str]:
    """Чем строка важнее, тем она выше: сначала упёршиеся, потом по расходу."""
    hit_the_wall = usage.refused > 0 or usage.exhausted
    return (0 if hit_the_wall else 1, -usage.spent, usage.model)
