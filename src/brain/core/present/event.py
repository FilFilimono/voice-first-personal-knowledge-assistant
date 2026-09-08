"""Контекст перед событием: то, что владелец читает за двадцать минут до встречи.

`vision.md` §5.7 — три слоя: что он сам просил напомнить, что известно
из архива по теме и человеку, что осталось незакрытым. **Отбор данных сюда
не заходит вовсе**: его целиком делает `core/digest/event_context`, а здесь
только слова.

Разделение труда — то же, что у лида сводки (ADR-0015, п. 5) и у связи
(`present/recall`), и повторяется оно не по инерции:

    решает код                                   пишет модель
    какие напоминания, записи и дела             одну фразу: зачем это сейчас
    порядок слоёв, даты, цитаты, якоря           —
    сколько времени до встречи                   —
    падение обратно на сухой рендер              —

**Сухой рендер — путь по умолчанию, а не заглушка.** `EVENT_CONTEXT_WRITTEN`
выключен, пока не включён биллинг: бесплатный тир даёт 20 запросов в сутки
на модель, и пятнадцать из них уже заняты чисткой, названиями и лидом сводки.
Включённый флаг ничего не ломает — фраза встаёт первой строкой, всё остальное
остаётся кодом, а при любом расхождении гвард возвращает прежний вид.

**Потолок вызовов в сутки задаётся кодом** (`EVENT_CONTEXT_DAILY_CAP`,
по умолчанию два) — требование ADR-0016. День с пятью встречами иначе съедает
квоту чистки расшифровки, и узналось бы это не отказом, а тем, что расшифровки
тихо перестали чиститься.

**Окно суток — тихоокеанское, как у всей квоты** (`core/quota`), а не полночь
владельца. Потолок стоит здесь ради суточного лимита провайдера, а он
сбрасывается в полночь Лос-Анджелеса; считать его по полуночи Рима значило бы
девять часов в сутки мерить не то окно. Ровно на этом месте в проекте уже
ломался тест придерживания косметики — «отказ час назад» уезжал во вчерашние
сутки провайдера.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from brain.config import get_settings
from brain.core import llm, quota
from brain.core.ports import EntryText, LanguageModel
from brain.core.present import entry as entry_view
from brain.core.present import guard
from brain.core.present.pacing import cosmetic_allowed
from brain.storage.db import session_scope
from brain.storage.models.llm_call import LlmCall

logger = logging.getLogger(__name__)

# Назначение вызова в `llm_calls`. Своё, как у чистки, названий, сводки
# и связей, — и здесь оно работает вдвойне: по нему же считается суточный
# потолок. Метка, разъехавшаяся с потолком, отключила бы потолок молча.
PURPOSE = "event_context"

# Разброс не нужен: фраза пишется по одной встрече и одному материалу,
# «другой вариант той же фразы» не значит ничего.
TEMPERATURE = 0.0

# Потолок написанного моделью. Фраза, а не абзац: под ней стоит материал,
# который владелец и должен прочитать, а до встречи пятнадцать минут.
WORD_BUDGET = 25

# Сколько символов показывать от строки материала. Больше, чем в сводке (70),
# потому что здесь смысл в том, чтобы вспомнить мысль, а не узнать её в списке.
SNIPPET = 200

# Сколько символов уходит модели от каждой строки. Медиана расшифровки 409,
# средняя 544 — большинство записей уезжает целиком.
CONTEXT = 600

# Чем модель отвечает «связи нет». Слово, а не пустая строка: пустую строку
# она возвращает и когда не поняла задачу, и отличить одно от другого нечем.
NOTHING = "нет"

# Заголовки слоёв. По-русски и от лица системы: владелец читает их за минуту
# до встречи, и «слой 1» ему ничего не скажет.
ASKED_TITLE = "Ты просил напомнить:"
SAID_TITLE = "Ты говорил об этом:"
UNFINISHED_TITLE = "Осталось незакрытым:"

PROMPT = """Ты пишешь одну фразу перед встречей владельца.

ГЛАВНОЕ ПРАВИЛО: фраза говорит, зачем ему этот материал прямо сейчас.
Пересказывать список не надо — он увидит его следующими строками.

Связи материала со встречей не видно — так и напиши одним словом «нет».
Натянутая связь стоит доверия ко всем остальным.

Нельзя: чисел, дат, имён и сроков, которых нет во входе; упрёка за то, чего
владелец не сделал; повелительного наклонения («не забудь», «подготовься»);
оценок и подбадриваний; кавычек, значков и списков.

Первой строкой идёт встреча, дальше — то, что система про неё нашла."""


@dataclass(frozen=True, slots=True)
class Item:
    """Одна строка материала вместе с тем, можно ли отдать её модели.

    Приватность едет рядом с текстом по тому же правилу, что в `ports.EntryText`
    и в `present.repository.TitleTarget`: тот, кто собирается отправить текст
    наружу, обязан узнать про неё из того же места, откуда взял текст, а не
    сходить за ней отдельно и забыть.

    **Приватная строка из показа не исчезает** — владельцу его собственная
    запись показывается (`vision.md` §5.3 про всплывание говорит ровно это),
    — но в модель не уходит (инвариант 5).

    `entry_id` и `created_at` есть только у материала из архива: у него одного
    есть оригинал, в который можно провалиться, и дата, которую надо назвать.
    """

    text: str
    is_private: bool = False
    entry_id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Meeting:
    """Встреча и три слоя того, что система про неё нашла.

    Порядок полей — порядок `vision.md` §5.7 и порядок в сообщении: сначала
    то, о чём владелец просил сам, потом его же прежние слова, потом
    незакрытое. Первое он получил по своей просьбе, третье требует действия,
    и требующее стоит последним не случайно: сообщение даёт, а не требует
    (§7.0, принцип 1).
    """

    title: str
    starts_at: datetime
    asked: tuple[Item, ...] = ()
    said: tuple[Item, ...] = ()
    unfinished: tuple[Item, ...] = ()

    @property
    def empty(self) -> bool:
        """Нечего показать ни в одном слое — молчим (`vision.md` §7.0, п. 3).

        Само по себе «через двадцать минут встреча» содержанием не является:
        владелец видит это в своём календаре, и сообщение об этом было бы
        сообщением ни о чём. Тот же довод, что у среза по темам в сводке.
        """
        return not (self.asked or self.said or self.unfinished)

    def items(self) -> tuple[Item, ...]:
        """Весь материал подряд, в порядке чтения."""
        return (*self.asked, *self.said, *self.unfinished)


def snippet(text: str, limit: int = SNIPPET) -> str:
    """Кусок текста одной строкой."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def render(meeting: Meeting, *, now: datetime, timezone: str, lead: str | None = None) -> str:
    """Контекст так, как его собирает код. Запасной путь и путь по умолчанию.

    Не удаляется никогда — ровно как сухой рендер сводки: он и есть та мягкая
    деградация, которой куплена недетерминированность. Приходит он, когда
    `EVENT_CONTEXT_WRITTEN` выключен, когда потолок вызовов исчерпан, когда
    косметика придержана квотой, когда модель не ответила и когда её ответ
    отверг гвард.
    """
    zone = ZoneInfo(timezone)
    lines = [f"🗓 {_left(meeting, now)}: {snippet(meeting.title, limit=80)}"]

    if lead:
        lines += ["", lead]

    lines += _layer(ASKED_TITLE, meeting.asked)
    lines += _quotes(meeting.said, zone=zone)
    lines += _layer(UNFINISHED_TITLE, meeting.unfinished)

    return "\n".join(lines)


def _left(meeting: Meeting, now: datetime) -> str:
    """Сколько осталось до встречи. Минутами, а не временем начала.

    Время начала владелец только что видел в календаре; ценность сообщения
    в том, что оно приходит **до**, и первое слово должно говорить именно это.
    """
    minutes = max(1, round((meeting.starts_at - now).total_seconds() / 60))
    return f"Через {minutes} мин"


def _layer(title: str, items: Sequence[Item]) -> list[str]:
    """Слой списком. Пустой слой не даёт даже заголовка: заголовка без списка
    не бывает."""
    if not items:
        return []
    return ["", title, *[f"— {snippet(item.text)}" for item in items]]


def _quotes(items: Sequence[Item], *, zone: ZoneInfo) -> list[str]:
    """Слова владельца из архива — с датой и якорем в оригинал.

    Якорь собирается в `present/entry`, одним местом на всех отправителей:
    `vision.md` §8 требует, чтобы из цитаты можно было провалиться в запись,
    а разъехавшаяся приставка сломала бы часть показов молча.
    """
    if not items:
        return []

    lines = ["", SAID_TITLE]
    for item in items:
        when = "" if item.created_at is None else f"{item.created_at.astimezone(zone):%d.%m.%Y}: "
        anchor = "" if item.entry_id is None else f"  {entry_view.anchor(item.entry_id)}"
        lines.append(f"{when}{snippet(item.text)}{anchor}")
    return lines


def response_schema() -> dict[str, Any]:
    """Схема ответа. Одно поле: модель пишет одну фразу и больше ничего."""
    return {
        "type": "object",
        "properties": {"phrase": {"type": "string"}},
        "required": ["phrase"],
    }


async def text(
    model: LanguageModel | None,
    *,
    meeting: Meeting,
    now: datetime,
    timezone: str,
) -> str:
    """Что владелец прочитает перед встречей. Пустого не возвращает никогда."""
    return render(meeting, now=now, timezone=timezone, lead=await write(model, meeting, now=now))


async def write(model: LanguageModel | None, meeting: Meeting, *, now: datetime) -> str | None:
    """Фраза от модели. `None` — уйдёт сухой рендер.

    `None` возвращается в семи случаях, и все семь законные: выключено
    настройкой, писать некому, показывать нечего, потолок вызовов за сутки
    исчерпан, косметика придержана квотой, модель не ответила или ответила
    «связи нет», ответ отверг гвард.
    """
    settings = get_settings()
    if not settings.event_context_written or model is None or meeting.empty:
        return None

    if not await cosmetic_allowed(now):
        # Под квотой косметика замолкает первой (ADR-0015, п. 6). Контекст
        # при этом придёт: сухой рендер стоит ноль запросов.
        return None

    spent = await calls_today(now)
    if spent >= settings.event_context_daily_cap:
        logger.info(
            "контекст: потолок вызовов за сутки исчерпан (%s из %s) — уйдёт сухой рендер",
            spent,
            settings.event_context_daily_cap,
        )
        return None

    entries = _input(meeting)
    if len(entries) < 2:
        # В пачке остались одна встреча и ничего больше: весь материал оказался
        # приватным. Писать не о чем, а вызов стоит столько же, сколько нужный.
        logger.info("контекст: показывать модели нечего — уйдёт сухой рендер")
        return None

    try:
        completion = await llm.complete_over_entries(
            model,
            entries=entries,
            system=f"{PROMPT}\n\nДлина: не больше {WORD_BUDGET} слов.",
            schema=response_schema(),
            temperature=TEMPERATURE,
            purpose=PURPOSE,
        )
    except Exception:
        # Отказ модели стоит фразы, а не контекста: он уйдёт сухим рендером
        # этим же тиком. Исключение отсюда наверх означало бы, что упавший
        # провайдер отменяет заказанное касание.
        logger.exception("фраза перед встречей не написана — уйдёт сухой рендер")
        return None

    phrase = str(completion.data.get("phrase", "")).strip()
    if not phrase or phrase.lower().strip(" .") == NOTHING:
        logger.info("модель не нашла, что сказать перед встречей, — уйдёт сухой рендер")
        return None

    refusal = guard.check(
        guard.Written(lead=phrase),
        # Идентификаторов модель не возвращает вовсе: строки материала пишет
        # код. Проверка остаётся на месте — она ловит ответ, в котором
        # появилось то, чего не просили.
        expected_ids=(),
        source_digits=_digits(entries),
        word_budget=WORD_BUDGET,
    )
    if refusal is not None:
        logger.info("фраза перед встречей отвергнута (%s): %r", refusal, phrase[:120])
        return None

    return phrase


def _input(meeting: Meeting) -> list[EntryText]:
    """Вход модели: встреча первой строкой, дальше материал. Приватное не едет.

    Номер в конверте — **порядковый номер строки пачки, а не идентификатор
    базы**: в одну пачку идут напоминания, записи и дела, и номер 5 значил бы
    сразу три разные строки. То же решение, что у лида сводки.

    Приватное отсеивается здесь, а не в двери `core/llm`: дверь на приватную
    запись поднимает исключение, и одна просочившаяся строка утащила бы
    за собой весь вызов.
    """
    items = [meeting.title, *[item.text for item in meeting.items() if not item.is_private]]
    return [
        EntryText(entry_id=number, text=snippet(item, limit=CONTEXT), is_private=False)
        for number, item in enumerate(items, start=1)
    ]


def _digits(items: Iterable[EntryText]) -> set[str]:
    """Числа, которые были во входе, — вместе с номерами строк конверта.

    Номера считаются наравне с текстом: модель видит их так же, как всё
    остальное, и запретить ей повторить единицу из «[1]» значило бы отвергать
    фразу за слово «один».
    """
    return guard.digits(" ".join(f"{item.entry_id} {item.text}" for item in items))


async def calls_today(now: datetime) -> int:
    """Сколько вызовов контекста сделано в текущих сутках провайдера.

    Считаются **все строки, а не только успешные**: потолок бережёт суточный
    лимит запросов, а отказ провайдера этот лимит тоже занял. `quota.used`
    считает иначе, и это не противоречие — там отвечают на вопрос «сколько
    полезного сделано», здесь на вопрос «сколько раз мы к нему сходили».

    Запрос живёт здесь, а не в `present/repository`, по одной причине: число
    и правило, по которому оно сравнивается с потолком, читаются вместе.
    Разнести их значит однажды объяснять, почему потолок два, а вызовов три.
    """
    async with session_scope() as session:
        counted = await session.scalar(
            select(func.count())
            .select_from(LlmCall)
            .where(LlmCall.purpose == PURPOSE, LlmCall.created_at >= quota.window_start(now))
        )
    return int(counted or 0)


__all__ = [
    "CONTEXT",
    "PROMPT",
    "PURPOSE",
    "SNIPPET",
    "WORD_BUDGET",
    "Item",
    "Meeting",
    "calls_today",
    "render",
    "response_schema",
    "text",
    "write",
]
