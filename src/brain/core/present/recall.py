"""Тексты возврата: связь в момент записи и срез по темам за период.

Оба текста собирает **код шаблоном**, а не модель, и это решение владельца
от 27.08.2026, а не осторожность. Счёт простой и он в плане Фазы 4: бесплатный
тир даёт 20 запросов в сутки на модель, из них 15 уже заняты чисткой,
названиями и лидом сводки. Связь срабатывает на **каждой** записи — при живом
темпе в 9.4 записи в день модельная фраза связи одна вынесла бы дневной
остаток целиком.

Место под модельную версию оставлено и **работает**: `RECALL_WRITTEN=true`
включает её без единой правки кода — так же, как `PRESENT_WORKING_TEXT`
и `CLEANUP_ENABLED`. Переключатель меняет поведение, а не намерение.

Разделение труда прежнее (ADR-0015, п. 5):

    решает код                         пишет модель
    какие связи и какие темы           одну фразу: чем именно перекликается
    даты, цитаты, порядок, значки      —
    падение обратно на шаблон          —

Правила у написанного здесь те же, что у лида сводки: проверка гвардом перед
показом, при любом расхождении — прежний шаблон, без второй попытки. Шаблон
поэтому не удаляется никогда: он и есть та мягкая деградация, которой куплена
недетерминированность.

**Ретроспективного упрёка тут нет по построению.** Связь показывает прежнюю
запись и не говорит о ней ничего оценочного: «вы записывали» — это факт,
а «вы так и не вернулись» было бы упрёком (`vision.md` §7.0, принцип 2).
Гвард ловит второе по лексике, но у шаблона ловить нечего — в нём нет ни одного
слова про несделанное.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from brain.config import get_settings
from brain.core import llm
from brain.core.ports import EntryText, LanguageModel
from brain.core.present import entry as entry_view
from brain.core.present import guard
from brain.core.present.pacing import cosmetic_allowed
from brain.core.retrieval.links import Link

logger = logging.getLogger(__name__)

# Назначение вызова в `llm_calls`. Своё, как у чистки, названий и сводки:
# вопрос «сколько стоят связи» без метки не спросить, а спросят его ровно
# в тот день, когда кончится квота.
PURPOSE = "recall"

# Разброс не нужен: фраза пишется по двум конкретным записям, и «другой вариант
# той же фразы» не значит ничего.
TEMPERATURE = 0.0

# Сколько символов прежней записи показывать. Больше, чем в сводке (70), потому
# что здесь смысл именно в том, чтобы вспомнить мысль, а не узнать её в списке;
# меньше, чем во всплывании (400), потому что связь приходит следом за ответом
# на запись и не должна собой этот ответ заслонять.
SNIPPET = 200

# Сколько текста уходит модели. Медиана расшифровки 409 символов, средняя 544 —
# большинство записей уезжает целиком, а длинная обрезается по началу: мысль
# в голосовой записи стоит в начале.
CONTEXT = 600

# Потолок написанного моделью. Фраза, а не абзац: она стоит перед цитатой,
# которую владелец и должен прочитать.
WORD_BUDGET = 25

# Что стоит в первой строке, когда фразу пишет код. Называет факт и не просит
# ничего: захочет вернуться — ответит сам, ответ на сообщение бота в этой
# системе и есть способ что-то сказать.
HEADLINE = "Перекликается с тем, что вы записывали раньше."

LINK_PROMPT = """Ты пишешь одну фразу, которая связывает новую запись владельца
с его же прежней записью.

ГЛАВНОЕ ПРАВИЛО: связь называется по существу. «Это перекликается с прошлой
записью» — пустая фраза, её владелец видит и без тебя; надо сказать, **чем
именно** перекликается: та же тема, тот же человек, то же решение.

Связи не видно — так и напиши одним словом «нет». Натянутая связь стоит
доверия ко всем остальным.

Нельзя: чисел, дат и имён, которых нет во входе; оценок, советов и
подбадриваний; упрёка за то, что владелец к этому не вернулся; повелительного
наклонения; кавычек и значков. Саму прежнюю запись цитировать не надо —
владелец увидит её следующей строкой.

Первой строкой идёт новая запись, дальше — прежние."""

TOPICS_PROMPT = """Ты пишешь одну фразу в начало среза по темам.

ГЛАВНОЕ ПРАВИЛО: фраза называет то, что видно из списка тем, и не пересказывает
его. Перечислять темы не надо — владелец увидит список следующей строкой.

Нельзя: чисел и процентов; оценок («много», «мало», «наконец-то»); упрёка
за то, к чему владелец не возвращался; повелительного наклонения; кавычек
и значков.

Тебе дают темы за период и сколько записей в каждой."""

# Чем модель отвечает «связи нет». Слово, а не пустая строка: пустую строку
# модель возвращает и когда не поняла задачу, и отличить одно от другого
# было бы нечем.
NOTHING = "нет"


def response_schema() -> dict[str, Any]:
    """Схема ответа. Одно поле: модель пишет одну фразу и больше ничего."""
    return {
        "type": "object",
        "properties": {"phrase": {"type": "string"}},
        "required": ["phrase"],
    }


def snippet(text: str, limit: int = SNIPPET) -> str:
    """Кусок записи одной строкой."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def quotes(links: Sequence[Link], *, timezone: str) -> list[str]:
    """Прежние записи с датами — то, ради чего связь и показывается.

    С 7 сентября 2026 у каждой цитаты якорь: связь показывает 200 символов
    старой записи, а `vision.md` §8 требует, чтобы в оригинал можно было
    провалиться. Собирается якорь в `present/entry` — одним местом на все
    шесть отправителей.
    """
    zone = ZoneInfo(timezone)
    return [
        f"{link.created_at.astimezone(zone):%d.%m.%Y}: {snippet(link.text)}  "
        f"{entry_view.anchor(link.entry_id)}"
        for link in links
    ]


def template(links: Sequence[Link], *, timezone: str) -> str:
    """Связь так, как её собирает код. Запасной путь и путь по умолчанию."""
    return "\n\n".join([f"🔗 {HEADLINE}", *quotes(links, timezone=timezone)])


async def link_text(
    model: LanguageModel | None,
    *,
    text: str,
    links: Sequence[Link],
    timezone: str,
    now: datetime,
) -> str:
    """Что владелец прочитает про связь. Пустого не возвращает никогда.

    `model` — `None` означает «писать некому», и это законное состояние: так
    связи зовут тесты, которые про отбор, а не про текст.

    Модель заменяет **только первую строку**: даты и цитаты прежних записей
    остаются кодом в любом случае. Она добавляет то, чего код не умеет, —
    говорит, чем именно перекликается.
    """
    if not links:
        return ""
    phrase = await written(
        model, prompt=LINK_PROMPT, items=[text, *[link.text for link in links]], now=now
    )
    return "\n\n".join([f"🔗 {phrase or HEADLINE}", *quotes(links, timezone=timezone)])


async def topics_text(
    model: LanguageModel | None,
    *,
    headline: str,
    listing: str,
    invitation: str,
    now: datetime,
) -> str:
    """Срез по темам. Данные собирает `core/digest/by_topic`, слова — здесь.

    Список тем и приглашение остаются кодом при любом раскладе: модель пишет
    только первую строку, и то лишь при `RECALL_WRITTEN=true`.
    """
    phrase = await written(model, prompt=TOPICS_PROMPT, items=[listing], now=now)
    return "\n\n".join([f"🧭 {phrase or headline}", listing, invitation])


async def written(
    model: LanguageModel | None,
    *,
    prompt: str,
    items: Sequence[str],
    now: datetime,
) -> str | None:
    """Фраза от модели. `None` — показывается шаблон.

    `None` возвращается в шести случаях, и все шесть законные: выключено
    настройкой, писать некому, показывать нечего, косметика придержана квотой,
    модель не ответила или ответила «связи нет», ответ отверг гвард.
    """
    if not get_settings().recall_written or model is None or not items:
        return None

    if not await cosmetic_allowed(now):
        # Под квотой косметика замолкает первой (ADR-0015, п. 6). Связь при
        # этом придёт: шаблон стоит ноль запросов.
        return None

    # Приватного здесь не бывает по построению: у приватной записи нет вектора
    # (инвариант 5), значит нет и связей, а цель связи отбирается запросом
    # с `is_private_now()`. Дверь `core/llm` проверяет это ещё раз сама.
    entries = [
        EntryText(entry_id=number, text=snippet(item, limit=CONTEXT), is_private=False)
        for number, item in enumerate(items, start=1)
    ]

    try:
        completion = await llm.complete_over_entries(
            model,
            entries=entries,
            system=f"{prompt}\n\nДлина: не больше {WORD_BUDGET} слов.",
            schema=response_schema(),
            temperature=TEMPERATURE,
            purpose=PURPOSE,
        )
    except Exception:
        # Отказ модели стоит фразы, а не связи: она уйдёт шаблоном.
        logger.exception("фраза не написана — уйдёт шаблон")
        return None

    phrase = str(completion.data.get("phrase", "")).strip()
    if not phrase or phrase.lower().strip(" .") == NOTHING:
        logger.info("модель не нашла, что сказать, — уйдёт шаблон")
        return None

    refusal = guard.check(
        guard.Written(lead=phrase),
        # Идентификаторов модель не возвращает вовсе: цитаты и даты ставит код.
        expected_ids=(),
        source_digits=guard.digits(" ".join(f"{item.entry_id} {item.text}" for item in entries)),
        word_budget=WORD_BUDGET,
    )
    if refusal is not None:
        logger.info("фраза отвергнута (%s): %r", refusal, phrase[:120])
        return None

    return phrase
