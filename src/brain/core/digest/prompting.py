"""Раскачка: вопрос, который вытаскивает мысль.

`vision.md` §5.9 и блок A плана Фазы 4. Диагноз владельца, ради которого
модуль существует, звучит так: **бот не даёт повода ему что-то сказать.**
Захват работает, возврата нет. Раскачка — механизм возврата в чистом виде.

**Вопросы не сочиняются из головы.** Это прямое требование §5.9, и оно
выполнено буквально: каждый вопрос набора несёт поле `method` — приём
из `docs/prompting_sources.md`, §5, — и поле `source` с разделом разбора,
где этот приём обоснован. Вопрос без опоры в набор не идёт; проверяется
тестом, а не намерением.

**Тон — решение владельца от 27.08.2026, одной строкой:**

    БОТ НЕ ОЦЕНИВАЕТ ЧЕЛОВЕКА, НО НАЗЫВАЕТ ФАКТЫ И СПРАШИВАЕТ ПРИЧИНУ.

«Ты бездельник» и «опять не сделал» отклонены владельцем прямым текстом.
Разница с разрешённым — не в резкости, а в том, требует сообщение отчёта
или предлагает разговор. Разбор источников даёт под это механизм, а не вкус
(§4.1): владелец, получивший упрёк, перестаёт отвечать честно, и система,
вся ценность которой в архиве, разрушает собственное сырьё.

**Отбор — код, речь — модель, и сегодня даже речь код** (принцип 12). Какой
вопрос, к какой теме, к какой записи и в какой день — считается запросами
к архиву, ноль вызовов модели. Формулировку в полном режиме пишет модель
(`core/present/question`), и ветка под это лежит за `PROMPTING_WRITTEN`,
выключенным по умолчанию.

**Набор урезан владельцем 6 сентября 2026 с восьми видов до пяти**, и два
оставшихся переписаны. Вычеркнуты `last_time` (те же данные, что у затихшей
темы, — пара вопросов через день), `own_side` и `intention` (оба стояли
на делах, а дела из утреннего сообщения уходят). Переписаны:

- **`look_back`** брал запись по возрасту, то есть выдавал старое, ни с чем
  не связанное, — ровно та претензия, ради которой набор и пересматривался.
  Теперь старая запись ищется **по близости к тому, о чём владелец говорит
  сейчас**; связного нет — вид молчит;
- **`blocker`** звучал «Что вчера помешало?» и был единственным без факта
  перед вопросом: предполагал провал до того, как он известен. Теперь это факт
  из данных плюс «что мешает?», а срабатывает он на теме, в которую владелец
  возвращается без движения.

Пять правил отбора, каждое со своей опорой:

- **из архива достаётся связанное, а не случайное** (решение владельца
  06.09.2026): молчание честнее несвязанной цитаты, потому что одна такая
  обесценивает и все остальные;

- **один вопрос в сообщении** (§1.3): раскачка молчит в тот день, когда
  сообщение слота уже о чём-то спрашивает — например, когда в сводке есть
  петля с её «Сдвинулось?»;
- **последовательность не фиксирована** (§2.2): рамка, пройденная как
  чек-лист, даёт поверхностную рефлексию. Поэтому выбирается не «первый
  подходящий», а **тот, которого дольше всех не было**;
- **цитируем только неприватное** (инвариант 5, решение владельца № 4):
  дословная цитата — самый дешёвый способ говорить его словами (§4.4),
  но цитата уходит в модель;
- **молчание не поминается вовсе** (решение владельца № 7): вопрос не
  повторяется, про молчание не говорится ни слова, а после трёх молчаний
  подряд частота снижается сама. Считается **по журналу касаний**, а не
  счётчиком в колонке: колонка, посчитавшая молчания, однажды прочиталась бы
  вслух — других судеб у числа в базе не бывает (`vision.md` §7.0, п.5).

**Чего в наборе нет намеренно.** Шкал и «change ruler» (§1.5, п.8) — это
счётчик, запрещённый §7.0 п.5, и владелец подтвердил отказ. Вопросов, стоящих
на списке дел, — вычеркнуты 06.09.2026 вместе с самим списком. Вопросов про то,
что зависит от других людей (§2.3) — такие подсказки не отзываются вовсе;
вместо сужения тем вопрос переформулируется **на часть владельца**: не «почему
созвон не состоялся», а «что ты успел со своей стороны». Вопроса «как
продвижение?» — это ровно тот общий вопрос, ради замены которого §5.9 и
написан.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select

from brain.config import PromptingSlot, Settings, get_settings
from brain.core.digest import slots
from brain.core.memory import overrides
from brain.core.memory.repository import display_text, is_private_now
from brain.core.ports import LanguageModel
from brain.core.present import guard
from brain.core.present import question as writer
from brain.core.retrieval import links
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.topic import EntryTopic, Topic
from brain.storage.models.touch import Touch, TouchSlot

logger = logging.getLogger(__name__)

# --- Слоты: настройка, а не константа ---
#
# Здесь только перевод имени из `.env` в значение колонки. Сам список живёт
# в `brain.config` (`PROMPTING_SLOTS`) — решение владельца № 2 от 27.08.2026,
# и до 6 сентября оно было нарушено: константа модуля значила, что переключить
# режим без правки кода нельзя. Перевод не в настройках, потому что
# `brain.config` не импортирует `brain.storage`.
_BY_NAME: dict[PromptingSlot, TouchSlot] = {
    "morning": TouchSlot.MORNING,
    "day": TouchSlot.DAY,
    "evening": TouchSlot.EVENING,
}


def slots_from(settings: Settings) -> frozenset[TouchSlot]:
    """В каких слотах спрашивать. Пусто — раскачка молчит везде.

    Пустое множество здесь законно и означает выключатель: `PROMPTING_SLOTS=`
    в `.env` — естественный способ сказать «не спрашивай», и он обязан работать
    без правки кода, ровно как пустая квота означает «лимит неизвестен».
    """
    return frozenset(_BY_NAME[name] for name in settings.prompting_slots)


# --- Пороги отбора ---

# Сколько дней тишины по теме делают её «затихшей». Три — из разрешённой
# владельцем формулировки «за три дня ни одной записи про учёбу».
QUIET_AFTER = timedelta(days=3)

# А после скольких спрашивать уже поздно. Месяц молчания по теме — это не
# «застряло», это отпало само, и вопрос про неё читался бы как напоминание
# о брошенном, то есть как упрёк.
FORGOTTEN_AFTER = timedelta(days=30)

# Сколько записей делают тему темой. Одна-две — это ещё не тема, а совпадение
# слов, и «за три дня ни одной записи про X» про такую звучит выдумкой.
MIN_TOPIC_ENTRIES = 3

# Сколько дней подряд считается возвращением к теме. Три — то же число, что
# в утверждении из §1.6 разбора: «ты три вечера подряд возвращался к главе
# про индексы». Это утверждение по фактам, а не похвала и не серия: названо
# конкретное действие, без оценки и без счёта достижений.
RETURN_DAYS = 3

# Насколько старше должна быть запись, чтобы «оглянуться назад» на неё имело
# смысл (§1.5, п.5). Ближе двух недель — это ещё не «назад», владелец такое
# помнит и сам.
#
# **Верхней границы больше нет, и это решение владельца 6 сентября 2026.**
# Здесь стояло `LOOK_BACK_MAX = 60 дней` — «дальше двух месяцев уже
# археология». Оно было верно, пока старая запись выбиралась по возрасту:
# случайная мысль полугодовой давности действительно не к месту. Теперь запись
# выбирается **по связи с тем, о чём владелец говорит сейчас**, и относимость
# обеспечивает связь, а не окно. Прошлогодняя запись про то же самое — это
# как раз то, ради чего система и заводилась.
LOOK_BACK_MIN = timedelta(days=14)

# Что считается «сейчас»: окно, из которого берётся запись, к которой ищется
# старая.
#
# **Неделя, а не три дня, и число выбрано живой проверкой 06.09.2026,
# а не рассуждением.** Первая редакция брала три дня — то же окно, что
# у затихшей темы. Прогон по рабочему архиву показал, что в этом окне
# **ноль записей**: последняя сделана 1 сентября, а сегодня шестое. То есть
# вид, названный владельцем главным, молчал бы в день собственного включения
# и молчал бы молча — ровно тот почерк, из-за которого Фаза 4 неделю выглядела
# работающей.
#
# Дальше недели окно не растягивается намеренно: молчание длиннее недели —
# это уже случай затихшей темы (`quiet_topic`), и у него свой вопрос.
RECENT = timedelta(days=7)

# Сколько свежих записей опрашивать, прежде чем сдаться. Каждая — один запрос
# к векторам, то есть цена перебора видна; десяти хватает на три дня живого
# темпа (9.4 записи в день), а связь находится у четырёх записей из пяти.
RECENT_TRIED = 10

# Окно, в котором тема считается «упоминается несколько раз». Неделя — то же
# окно, что у среза по темам: меньше — и три упоминания собираются редко,
# больше — и «сейчас» перестаёт быть сейчас.
STUCK_WINDOW = timedelta(days=7)

# Что считается движением по теме. Решение и понимание — единственные два типа,
# в которых владелец сам говорит, что сдвинулся; остальные семь этого не
# говорят, включая `task`: заведённая задача — это ещё не движение, а его
# намерение.
MOVED: frozenset[AnnotationType] = frozenset({AnnotationType.DECISION, AnnotationType.INSIGHT})

# Короче этого не цитируется: «ок», «в 14:50», проверка бота — там нечего
# вспоминать, а вытащенный обрывок читается как сбой. То же число, что
# у всплывания старого.
MIN_LENGTH = 100

# Сколько символов цитаты показывать. Больше, чем в сводке (70), потому что
# смысл именно в том, чтобы узнать свою мысль; меньше, чем во всплывании (400),
# потому что цитата здесь — подводка к вопросу, а не сама запись.
QUOTE = 180

# Сколько молчаний подряд снижают частоту и насколько. Оба числа выбраны,
# а не измерены, и это надо знать: про частоту раскачки источников нет вовсе
# (`docs/prompting_sources.md`, §7, п.4). Первый способ их проверить — неделя
# использования и доля слотов, на которые пришёл ответ.
SILENCE_STREAK = 3
QUIET_GAP_DAYS = 3


@dataclass(frozen=True, slots=True)
class Question:
    """Готовый вопрос вместе с тем, из чего он собран.

    `entry_ids` — записи архива, на которых вопрос стоит. Они уезжают в журнал
    касаний и возвращаются оттуда в ответ владельца: без них раскачка даёт
    повод сказать и теряет, **на что** было сказано.
    """

    kind: str
    method: str
    text: str
    # Факт, на котором стоит вопрос, — то, что владелец читает **до** него.
    # Пустая строка означает, что факт вошёл в сам вопрос и отдельной части
    # у него нет; так устроены четыре вида из пяти.
    #
    # Отдельной частью он появился 6 сентября 2026 вместе с новым порядком
    # утреннего сообщения: материал сверху, вопрос в конце (решение владельца).
    # Разделять пришлось потому, что это одна вещь на два места в сообщении,
    # а не две разные: цитата из архива и вопрос про неё.
    material: str = ""
    entry_ids: tuple[int, ...] = ()
    # То, из чего собран вопрос, — вход модельной ветки. Приватного здесь
    # не бывает: отбор берёт цитаты запросом с `~is_private_now()`.
    facts: tuple[str, ...] = ()

    @property
    def whole(self) -> str:
        """Вопрос вместе с фактом — там, где они идут одним куском.

        Так он уходит везде, кроме утреннего сообщения: в слоте, которому
        больше нечего сказать, вопрос обязан нести свой факт с собой, иначе
        «Что с тех пор изменилось?» приходит само по себе и не значит ничего.
        """
        return f"{self.material} {self.text}" if self.material else self.text


Builder = Callable[[datetime, str], Awaitable[Question | None]]


@dataclass(frozen=True, slots=True)
class Item:
    """Один вопрос набора: шаблон, приём, опора и данные, на которых он стоит.

    `method` и `source` — не документация, а часть набора: `vision.md` §5.9
    требует, чтобы вопросы составлялись по исследованным практикам, и проверить
    это можно только тем, что опора лежит рядом с вопросом и читается вместе
    с ним. Пустое поле роняет тест.
    """

    method: str
    source: str
    template: str
    build: Builder
    # Шаблон факта, который в утреннем сообщении показывается **отдельно
    # и выше** вопроса. Пусто — факт входит в сам вопрос.
    fact: str = ""
    # Чем подставляются поля шаблона, когда вопрос показывают вне архива:
    # в тестах и в отчёте владельцу. Выдуманы намеренно — живая речь в git
    # не уходит (ADR-0002).
    sample: Mapping[str, str] = field(default_factory=dict)


# --- Помощники отбора ---


def quote(text: str) -> str:
    """Цитата одной строкой. Дословно — но в пределах строки сообщения."""
    flat = " ".join(text.split())
    if len(flat) <= QUOTE:
        return flat
    return flat[: QUOTE - 1].rstrip(" ,.;:—-") + "…"


def acceptable(text: str) -> bool:
    """Годится ли готовый вопрос к показу.

    Гвард гоняется и по тексту, который собрал код, а не только по тексту
    модели, и это не перестраховка: в вопрос приезжает **дословная цитата**
    владельца, а в его собственной речи «забыл» и «так и не» — обычные слова.
    В его записи это речь, в сообщении бота — уже упрёк.
    """
    found = guard.reproach(text)
    if found is not None:
        logger.info("вопрос раскачки отброшен (упрёк «%s»): %r", found, text[:120])
    return found is None


def fits(text: str) -> bool:
    """Есть ли в сообщении место для вопроса.

    Один вопрос в сообщении — источники единогласны (`docs/prompting_sources.md`,
    §1.3, ловушка допроса). Считается знаками вопроса в уже собранном тексте
    слота, а не перебором случаев: случаев со временем станет больше, а правило
    останется тем же.
    """
    return "?" not in text


def attach(text: str, asked: str) -> str:
    """Приписать вопрос к сообщению слота — отбитым и со своим значком."""
    return f"{text}\n\n{alone(asked)}"


def alone(asked: str) -> str:
    """Вопрос сам по себе — когда слоту больше нечего сказать."""
    return f"💬 {asked}"


# --- Данные, на которых стоят вопросы ---


@dataclass(frozen=True, slots=True)
class _Recorded:
    """Запись архива в том виде, в каком её видит отбор."""

    entry_id: int
    text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Gap:
    """Затихшая тема и последняя запись по ней."""

    topic: str
    days: int
    last: _Recorded


async def _silent_topic(now: datetime) -> _Gap | None:
    """Тема, по которой давно нет записей. `None` — таких нет.

    Порог с двух сторон: молчание короче трёх дней ещё ничего не значит,
    длиннее месяца уже не вопрос, а напоминание о брошенном.
    """
    last_seen = func.max(Entry.created_at)

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Topic.name, last_seen)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .group_by(Topic.name)
                .having(func.count() >= MIN_TOPIC_ENTRIES)
                .having(last_seen <= now - QUIET_AFTER)
                .having(last_seen >= now - FORGOTTEN_AFTER)
                .order_by(last_seen.desc())
                .limit(1)
            )
        ).first()

    if row is None:
        return None

    last = await _last_of_topic(str(row[0]))
    if last is None:
        # Записи темы есть, но все приватные: цитировать нечего, и факт про
        # тему тоже не называем — тема приватной записи всё ещё её содержание.
        return None

    return _Gap(topic=str(row[0]), days=max(1, (now - row[1]).days), last=last)


async def _last_of_topic(
    name: str, *, since: datetime | None = None, quotable: bool = False
) -> _Recorded | None:
    """Последняя **неприватная** запись темы. Она же — цитата и связь.

    Две поправки, и обе заведены по разбору тестов 06.09.2026 — то есть найдены
    чтением кода вокруг новых тестов, а не падением:

    - **`since`** ограничивает окном. Без него `blocker` мог процитировать
      запись месячной давности, назвав её словами «тема, в которую ты
      возвращаешься»: тема набрала три записи за неделю, а последняя
      **неприватная** оказалась старше окна. Факт при этом остался бы верным,
      а цитата — не из того времени, о котором речь;
    - **`quotable`** требует длины. Там, где текст показывается владельцу,
      «ок» или «в 14:50» читаются как сбой, а не как его мысль. Тот же порог,
      что у всплывания и у цитат раскачки.

    Оба выключены по умолчанию намеренно: `quiet_topic` и `returning` берут
    отсюда только номер записи и не цитируют её вовсе, а лишний порог сделал
    бы их молчаливыми там, где сказать есть что.
    """
    body = display_text()

    async with session_scope() as session:
        query = (
            select(Entry.id, body, Entry.created_at)
            .select_from(EntryTopic)
            .join(Topic, Topic.id == EntryTopic.topic_id)
            .join(Entry, Entry.id == EntryTopic.source_entry_id)
            .where(Topic.name == name, ~is_private_now(), body.is_not(None))
            .order_by(Entry.created_at.desc())
            .limit(1)
        )
        if since is not None:
            query = query.where(Entry.created_at >= since)
        if quotable:
            query = query.where(func.length(body) >= MIN_LENGTH)
        row = (await session.execute(query)).first()

    if row is None:
        return None
    return _Recorded(entry_id=int(row[0]), text=str(row[1]), created_at=row[2])


async def _returned_topic(now: datetime, timezone: str) -> tuple[str, _Recorded] | None:
    """Тема, к которой владелец возвращался несколько дней подряд.

    Дни считаются по календарю владельца, а не «минус сутки»: три захода
    за одни сутки — это один день, а не три, и утверждение «три дня подряд»
    в таком случае было бы неправдой.
    """
    since = now - timedelta(days=RETURN_DAYS + 1)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.name, Entry.created_at)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Entry.created_at >= since, Entry.created_at <= now)
            )
        ).all()

    seen: dict[str, set[date]] = {}
    for name, created in rows:
        seen.setdefault(str(name), set()).add(slots.local_date(created, timezone))

    today = slots.local_date(now, timezone)
    wanted = {today - timedelta(days=shift) for shift in range(1, RETURN_DAYS + 1)}

    for name in sorted(seen):
        if wanted <= seen[name]:
            last = await _last_of_topic(name)
            if last is not None:
                return name, last
    return None


async def _recorded(*, since: datetime, until: datetime, newest: bool = True) -> _Recorded | None:
    """Одна неприватная запись из окна, годная к цитированию.

    Детерминированно, а не случайно: у всплывания случайность — замысел
    (`vision.md` §5.3), а здесь вопрос строится **на конкретной записи**,
    и тест, проверяющий случайный выбор, проверяет только случайность.
    """
    body = display_text()
    order = Entry.created_at.desc() if newest else Entry.created_at.asc()

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Entry.id, body, Entry.created_at)
                .where(
                    Entry.created_at >= since,
                    Entry.created_at <= until,
                    ~is_private_now(),
                    func.length(body) >= MIN_LENGTH,
                )
                .order_by(order)
                .limit(1)
            )
        ).first()

    if row is None:
        return None
    return _Recorded(entry_id=int(row[0]), text=str(row[1]), created_at=row[2])


async def _lately(now: datetime, limit: int) -> list[_Recorded]:
    """Последние записи — то, «про что владелец сейчас». Свежие первыми.

    Нужны связанному всплыванию: старая запись выбирается не случайно, а по
    близости к тому, о чём владелец говорил на днях. Отбор здесь тот же, что
    у цитаты, — неприватные и не короче `MIN_LENGTH`.
    """
    body = display_text()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, body, Entry.created_at)
                .where(
                    Entry.created_at >= now - RECENT,
                    Entry.created_at <= now,
                    ~is_private_now(),
                    func.length(body) >= MIN_LENGTH,
                )
                .order_by(Entry.created_at.desc())
                .limit(limit)
            )
        ).all()

    return [_Recorded(entry_id=int(row[0]), text=str(row[1]), created_at=row[2]) for row in rows]


async def _subject(entry_id: int) -> str | None:
    """Тема записи — то, чем в сообщении называется «про что ты сейчас».

    `None` — темы нет, и тогда вид молчит на этой записи и пробует следующую.
    Подставлять вместо темы обрывок текста нельзя: «ты сейчас про надо бы
    посмотреть» — это не название предмета, а середина фразы.
    """
    async with session_scope() as session:
        name = await session.scalar(
            select(Topic.name)
            .select_from(EntryTopic)
            .join(Topic, Topic.id == EntryTopic.topic_id)
            .where(EntryTopic.source_entry_id == entry_id)
            .order_by(Topic.name)
            .limit(1)
        )
    return None if name is None else str(name)


async def _stuck_topic(now: datetime) -> _Recorded | None:
    """Тема, о которой владелец говорит и в которой ничего не сдвинулось.

    Возвращается **последняя запись** такой темы: она и есть факт, с которого
    начинается вопрос.

    **Что здесь считается движением, и почему именно это.** Решение владельца
    6 сентября 2026: условие срабатывания — «одна и та же тема упоминается
    несколько раз без движения», а не «вчера была запись и есть дело в работе».
    Прежнее условие предполагало провал до того, как он известен, и вопрос
    «что вчера помешало?» получался приговором с вопросительным знаком.

    Движение берётся из разметки, а не из состояния дел: тема, по которой
    за неделю есть решение (`decision`) или понимание (`insight`), сдвинулась
    **по словам самого владельца**. Тема, где за неделю три и больше
    упоминаний и ни одного из этих двух, — это возвращение к одному и тому же
    без вывода. Счётчик при этом не показывается и в текст не попадает
    (`vision.md` §7.0, п.5): число решает, спрашивать ли, а сказано будет
    только «ты писал: …».

    Тип спрашивается по одной записи через `overrides.current_type` — там же,
    где его спрашивают связи, и по той же причине: это единственное место,
    где поправка владельца перебивает ответ модели, и второе такое выражение
    в общем запросе разъехалось бы с ним молча.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.name, func.array_agg(Entry.id))
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Entry.created_at >= now - STUCK_WINDOW, Entry.created_at <= now)
                .group_by(Topic.name)
                .having(func.count() >= MIN_TOPIC_ENTRIES)
                .order_by(func.count().desc(), Topic.name)
            )
        ).all()

    for name, entry_ids in rows:
        moved = False
        for entry_id in entry_ids:
            if await overrides.current_type(int(entry_id)) in MOVED:
                moved = True
                break
        if moved:
            continue
        # Цитата — из того же окна, о котором говорит вопрос, и достаточно
        # длинная, чтобы её можно было узнать. Разбор — в `_last_of_topic`.
        last = await _last_of_topic(str(name), since=now - STUCK_WINDOW, quotable=True)
        if last is not None:
            return last
    return None


def _yesterday(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    """Границы вчерашнего дня по календарю владельца, а не «минус сутки»."""
    zone = ZoneInfo(timezone)
    day = (now.astimezone(zone) - timedelta(days=1)).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def _days(count: int) -> str:
    """Число дней по-русски. «За 1 дня» выдаёт шаблон с головой."""
    tail = count % 10
    hundreds = count % 100
    if tail == 1 and hundreds != 11:
        return f"{count} день"
    if tail in (2, 3, 4) and hundreds not in (12, 13, 14):
        return f"{count} дня"
    return f"{count} дней"


def _when(moment: datetime, timezone: str) -> str:
    """Дата словами сообщения — в календаре владельца."""
    return f"{moment.astimezone(ZoneInfo(timezone)):%d.%m}"


def _about(subject: str, moment: datetime, now: datetime, timezone: str) -> str:
    """Первая половина связанного всплывания: про что владелец говорил.

    **«Сейчас» говорится только тогда, когда это правда.** Владелец записал
    вопрос как «Ты сейчас про X», и на живом архиве это неверно чаще, чем
    кажется: проверка 06.09.2026 нашла последнюю запись пятидневной давности.
    Сказать «ты сейчас про разработку» про позапрошлую среду — это выдумать
    факт, а факт в этом наборе несущая часть, а не подводка.

    Граница — сутки: сегодняшнее и вчерашнее владелец держит в голове как
    текущее, а всё, что дальше, называется датой.

    **Тема берётся в кавычки и в именительном падеже, а не подставляется
    в предлог.** «Ты сейчас про учёба» — то, что вышло бы иначе: имена тем
    склоняет русский язык, а не `str.format`. Кавычки здесь не украшение,
    а единственный способ поставить неизменённое слово в живую фразу; тем же
    приёмом пользуются остальные виды набора («по теме „учёба“»).
    """
    zone = ZoneInfo(timezone)
    days = (now.astimezone(zone).date() - moment.astimezone(zone).date()).days
    if days <= 1:
        return f"Ты сейчас про тему «{subject}»"
    return f"{_when(moment, timezone)} у тебя была запись по теме «{subject}»"


def _said(moment: datetime, now: datetime, timezone: str) -> str:
    """«Вчера» или дата — тем словом, каким это назвал бы человек.

    Вчерашнее называется вчерашним: «05.09 ты писал» про вчера читается
    канцелярски, а вопрос и без того держится на том, что владелец узнаёт
    в цитате себя. Дальше вчерашнего — дата, потому что «позавчера»
    и «на прошлой неделе» уже требуют считать в уме.
    """
    zone = ZoneInfo(timezone)
    if moment.astimezone(zone).date() == (now.astimezone(zone) - timedelta(days=1)).date():
        return "Вчера"
    return _when(moment, timezone)


# --- Сам набор ---
#
# Порядок здесь — приоритет при равенстве, а не расписание: выбирается
# не первый подходящий, а тот, которого дольше всех не было (см. `pick`).
# Первым стоит вопрос, разобранный владельцем как образец всего набора.

QUIET_TOPIC = "quiet_topic"
LOOK_BACK = "look_back"
YESTERDAY = "yesterday"
RETURNING = "returning"
BLOCKER = "blocker"


async def _build_quiet_topic(now: datetime, timezone: str) -> Question | None:
    found = await _silent_topic(now)
    if found is None:
        return None
    return _make(
        QUIET_TOPIC,
        entry_ids=(found.last.entry_id,),
        facts=(found.last.text,),
        days=_days(found.days),
        topic=found.topic,
    )


async def _build_look_back(now: datetime, timezone: str) -> Question | None:
    """Старое, **связанное с тем, о чём владелец говорит сейчас**.

    **Переписано 6 сентября 2026 решением владельца, и это правка важнее
    самого вычёркивания.** Прежняя редакция брала запись по возрасту — любую
    из окна 14–60 дней, — то есть делала ровно то, на что владелец жаловался:
    «то, что бот пишет из архива, не несёт никакого смысла, оно ни с чем
    не связано».

    Теперь порядок обратный: берётся то, о чём он говорил на днях, и к нему
    ищется похожее в старом — **векторной близостью, без вызова модели**
    (инвариант 8). Механизм тот же, что у связей в момент записи
    (`core/retrieval/links`), с двумя поправками: цель обязана быть старше
    на две недели, а не на сутки, и связь нужна одна.

    **Связного не нашлось — вид молчит, а не выдаёт случайное.** Это прямое
    указание владельца, и оно сильнее желания заполнить слот: несвязанная
    цитата обесценивает и все остальные.
    """
    rules = replace(links.from_settings(get_settings()), min_age=LOOK_BACK_MIN, limit=1)

    for recent in await _lately(now, RECENT_TRIED):
        found = await links.related(recent.entry_id, rules=rules)
        if not found:
            continue
        subject = await _subject(recent.entry_id)
        if subject is None:
            # Тема — это «про что ты сейчас». Записи без темы пропускаем:
            # подставить вместо неё обрывок значит сказать бессмыслицу.
            continue
        old = found[0]
        return _make(
            LOOK_BACK,
            # Обе записи: и свежая, на которой стоит «сейчас», и старая,
            # которую цитируем. По ним ответ владельца связывается с тем,
            # на что он отвечал.
            entry_ids=(recent.entry_id, old.entry_id),
            facts=(recent.text, old.text),
            lately=_about(subject, recent.created_at, now, timezone),
            when=_when(old.created_at, timezone),
            quote=quote(old.text),
        )
    return None


async def _build_yesterday(now: datetime, timezone: str) -> Question | None:
    start, end = _yesterday(now, timezone)
    found = await _recorded(since=start, until=end)
    if found is None:
        return None
    return _make(
        YESTERDAY,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        quote=quote(found.text),
    )


async def _build_returning(now: datetime, timezone: str) -> Question | None:
    found = await _returned_topic(now, timezone)
    if found is None:
        return None
    name, last = found
    return _make(
        RETURNING,
        entry_ids=(last.entry_id,),
        facts=(last.text,),
        days=_days(RETURN_DAYS),
        topic=name,
    )


async def _build_blocker(now: datetime, timezone: str) -> Question | None:
    """Тема, в которую владелец возвращается и в которой ничего не сдвинулось.

    **Переписано 6 сентября 2026 решением владельца.** Прежний вопрос звучал
    «Что вчера помешало?» и был единственным в наборе без факта перед вопросом:
    он предполагал, что что-то не вышло, ещё до того как это известно.
    Владелец назвал это прямо, и правка ровно та же, что у всего набора, —
    **факт из данных, потом открытый вопрос**: сначала его собственные слова,
    потом «что мешает?».

    Условие срабатывания поменялось вместе с текстом, и это половина правки:
    было «вчера была запись и есть дело в работе», стало «одна и та же тема
    упоминается несколько раз без движения». Тогда факт настоящий,
    а не предполагаемый — см. `_stuck_topic`.

    Спрашивается **препятствие, а не вина**: «что мешает» — про обстоятельства,
    «почему не сделал» — про человека, и второе запрещено §4.1 и §4.2 разбора.
    """
    found = await _stuck_topic(now)
    if found is None:
        return None
    return _make(
        BLOCKER,
        entry_ids=(found.entry_id,),
        facts=(found.text,),
        when=_said(found.created_at, now, timezone),
        quote=quote(found.text),
    )


SET: dict[str, Item] = {
    # Первым — связанное всплывание. Порядок здесь работает только при
    # равенстве, но именно этот вид владелец назвал делающим главную работу
    # (решение 06.09.2026), поэтому в первые дни, пока ни один вопрос ещё
    # не задавали, спрашивается он.
    LOOK_BACK: Item(
        method="4 · оглянуться назад по архиву; 7 · говорить его словами",
        source="§1.5, п. 5 — Look Back · §4.4 чистый язык · связь по §5.4 vision",
        # Единственный вид, у которого факт вынесен отдельно: в утреннем
        # сообщении он и есть «материал сверху», а вопрос уходит в конец
        # (порядок сообщения — решение владельца 06.09.2026).
        fact="{lately}. А {when} писал: «{quote}»",
        template="Что с тех пор изменилось?",
        build=_build_look_back,
        sample={
            "lately": "Ты сейчас про тему «учёба»",
            "when": "06.08",
            "quote": "надо разобраться с индексами",
        },
    ),
    QUIET_TOPIC: Item(
        method="1 · назвать факт, не человека; 5 · дать выбор; 11 · факт, потом вопрос",
        source="§4.2 наблюдение против оценки · §1.2 праведный рефлекс · §2.3 MindScape",
        template="За {days} ни одной записи по теме «{topic}» — застряло или отпало?",
        build=_build_quiet_topic,
        sample={"days": "3 дня", "topic": "учёба"},
    ),
    YESTERDAY: Item(
        method="2 · привязать к моменту, а не к периоду; 7 · говорить его словами",
        source="§3.2 конкретный эпизод против периода · §4.4 чистый язык",
        template="Вчера ты записал: «{quote}». Что с этим сегодня?",
        build=_build_yesterday,
        sample={"quote": "надо разобраться с индексами"},
    ),
    RETURNING: Item(
        method="6 · утверждение по фактам вместо похвалы, «ты», а не «я»",
        source="§1.6 affirmation — конкретное действие без оценки",
        template="Ты возвращался к теме «{topic}» {days} подряд. Что там сдвинулось?",
        build=_build_returning,
        sample={"topic": "индексы", "days": "3 дня"},
    ),
    BLOCKER: Item(
        method="11 · сначала факт, потом открытый вопрос; 7 · говорить его словами",
        source="§2.3 MindScape · §4.1 blameless · решение владельца от 06.09.2026",
        template="{when} ты писал: «{quote}». Что мешает?",
        build=_build_blocker,
        sample={"when": "Вчера", "quote": "надо разобраться с индексами"},
    ),
}


def _make(
    kind: str, *, entry_ids: Sequence[int] = (), facts: Sequence[str] = (), **fields: str
) -> Question:
    """Собрать вопрос по шаблону набора."""
    item = SET[kind]
    return Question(
        kind=kind,
        method=item.method,
        text=item.template.format(**fields),
        material=item.fact.format(**fields) if item.fact else "",
        entry_ids=tuple(entry_ids),
        facts=tuple(facts),
    )


def sample(kind: str, **overrides: str) -> str:
    """Вопрос, собранный на образцах. Нужен тестам и отчёту владельцу.

    Лишние поля шаблону не мешают: `format` берёт только те, что в нём есть, —
    поэтому один и тот же набор подстановок годится всему набору сразу.

    Факт и вопрос склеиваются: в отчёте владельцу вид показывается целиком,
    а не половиной. То, что в утреннем сообщении они стоят в разных местах, —
    свойство сообщения, а не вида.
    """
    item = SET[kind]
    fields = {**item.sample, **overrides}
    asked = item.template.format(**fields)
    return f"{item.fact.format(**fields)} {asked}" if item.fact else asked


# --- Журнал: что спрашивали и что на это ответили ---


async def _asked_days() -> dict[str, date]:
    """Когда каждый вопрос набора спрашивали последний раз.

    По журналу касаний, а не по своей таблице: журнал уже хранит, что именно
    ушло, и вторая копия однажды разошлась бы с первой молча — ровно как
    разошлась бы таблица перевода типов, от которой отказался ADR-0011.
    """
    # Группировка по псевдониму, а не по повтору выражения: одно и то же
    # обращение к JSONB SQLAlchemy рендерит с разными плейсхолдерами, и Postgres
    # считает их **разными** выражениями — «column touches.payload must appear
    # in the GROUP BY clause». Псевдоним однозначен и читается лучше.
    kind = Touch.payload["prompting"]["kind"].astext.label("kind")

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(kind, func.max(Touch.local_date))
                .where(Touch.sent_at.is_not(None), Touch.payload.has_key("prompting"))
                .group_by(literal_column("kind"))
            )
        ).all()
    return {str(row[0]): row[1] for row in rows if row[0] is not None}


async def _recent(limit: int) -> list[tuple[int, date]]:
    """Последние заданные вопросы: строка журнала и её сутки. Свежие первыми."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Touch.id, Touch.local_date)
                .where(Touch.sent_at.is_not(None), Touch.payload.has_key("prompting"))
                .order_by(Touch.local_date.desc())
                .limit(limit)
            )
        ).all()
    return [(int(row[0]), row[1]) for row in rows]


async def _answered(touch_ids: Sequence[int]) -> bool:
    """Ответил ли владелец хоть на один из этих вопросов.

    Ответ — это запись архива со ссылкой на строку журнала (`meta.answers`).
    Отдельной таблицы под связку нет: ответ и есть запись, а связь живёт
    рядом с ней, там же, где `ack_message_id`.
    """
    if not touch_ids:
        return False
    async with session_scope() as session:
        found = await session.scalar(
            select(Entry.id)
            .where(Entry.meta["answers"]["touch_id"].astext.in_([str(item) for item in touch_ids]))
            .limit(1)
        )
    return found is not None


async def _due(day: date) -> bool:
    """Спрашивать ли сегодня вообще.

    Три молчания подряд снижают частоту — **и ни одного слова об этом**
    (решение владельца № 7). Снижение считается по журналу и снимается само,
    как только владелец ответит: отдельного состояния «мы обиделись» в системе
    нет и быть не должно.
    """
    recent = await _recent(SILENCE_STREAK)
    if len(recent) < SILENCE_STREAK:
        return True
    if await _answered([touch_id for touch_id, _ in recent]):
        return True

    since = (day - recent[0][1]).days
    if since < QUIET_GAP_DAYS:
        logger.info("раскачка придержана: %s молчаний подряд, прошло %s дн.", len(recent), since)
        return False
    return True


# --- Что спросить сегодня ---


async def pick(
    *,
    now: datetime,
    timezone: str,
    model: LanguageModel | None = None,
) -> Question | None:
    """Вопрос на сегодня. `None` — спрашивать нечего или незачем.

    **Выбирается тот, которого дольше всех не было**, а не первый подходящий.
    Причина в разборе (§2.2): фиксированная последовательность вопросов
    превращается в чек-лист, а рефлексия по чек-листу выходит поверхностной.
    Заодно это и есть правило «вопрос не повторять» из решения владельца № 7:
    вчерашний вопрос сегодня оказывается последним в очереди сам, без отдельной
    проверки на повтор.

    Порядок набора участвует только при равенстве — когда ни один из вопросов
    ещё не задавали. Первым в наборе стоит связанное всплывание (`LOOK_BACK`):
    владелец назвал его делающим главную работу и попросил дать ему **больший
    вес, а не жёсткую последовательность** (решение 06.09.2026). Вес — это
    ровно и есть место в наборе: спрошенный вчера, он уходит в конец очереди
    наравне со всеми.
    """
    if not await _due(slots.local_date(now, timezone)):
        return None

    asked = await _asked_days()

    candidates: list[tuple[date, int, Question]] = []
    for order, (kind, item) in enumerate(SET.items()):
        found = await item.build(now, timezone)
        # Гвард гоняется по вопросу **вместе с фактом**: цитата владельца живёт
        # именно в факте, и проверять один вопрос значило бы не проверять
        # ровно ту часть, ради которой гвард здесь и стоит.
        if found is None or not acceptable(found.whole):
            continue
        candidates.append((asked.get(kind, date.min), order, found))

    if not candidates:
        logger.info("раскачке нечего спросить: подходящих данных в архиве нет")
        return None

    chosen = min(candidates, key=lambda row: (row[0], row[1]))[2]
    return await _written(chosen, model=model, now=now)


async def _written(found: Question, *, model: LanguageModel | None, now: datetime) -> Question:
    """Полный режим: тот же вопрос, написанный моделью.

    Ветка выключена по умолчанию (`PROMPTING_WRITTEN`), но написана
    и вызывается отсюда, с пути тика: код за флагом, который никто не зовёт, —
    это `loops.is_stuck`, и стоила она проекту месяца неработавшей смены
    вопроса у петли.

    Отказ модели, придержанная квота и отвергнутый гвардом ответ дают один
    и тот же исход — прежний шаблон кода. Он поэтому не удаляется никогда.

    **Модель переписывает вопрос, а факт остаётся кодом.** Факт — это дословная
    цитата владельца и дата из базы; отдать их модели значило бы разрешить ей
    править услышанное имя и подвинуть дату, а отличить верную починку
    от выдумки гвард не может в принципе (решение владельца 27.08.2026,
    те же доводы, что у названий дел).
    """
    if not get_settings().prompting_written:
        return found

    phrase = await writer.written(
        model,
        method=found.method,
        fallback=found.text,
        facts=found.facts,
        now=now,
    )
    written = replace(found, text=phrase or "")
    if phrase is None or not acceptable(written.whole):
        return found
    return written


def payload(found: Question) -> dict[str, Any]:
    """Что кладётся в журнал касаний вместе с вопросом.

    Записи здесь — не украшение: по ним ответ владельца связывается с тем,
    на что он отвечал. `message_id` дописывается после отправки (см. `service`).
    """
    return {"kind": found.kind, "entry_ids": list(found.entry_ids)}


# --- Ответ владельца ---


@dataclass(frozen=True, slots=True)
class Answer:
    """То, на что владелец сейчас отвечает."""

    touch_id: int
    kind: str
    entry_ids: tuple[int, ...]

    def as_meta(self) -> dict[str, Any]:
        """Связь в том виде, в каком она ложится в `entries.meta`."""
        return {
            "touch_id": self.touch_id,
            "kind": self.kind,
            "entry_ids": list(self.entry_ids),
        }


async def answered(message_id: int) -> Answer | None:
    """Вопрос, на сообщение которого отвечают. `None` — это не ответ раскачке.

    Ищется по идентификатору сообщения в журнале — тем же приёмом, каким ответ
    на «когда напомнить?» ищется по `ack_message_id` в `entries.meta`. Своей
    таблицы ожидающих вопросов в проекте нет и здесь не заводится: состояние
    вопроса живёт в самой переписке.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Touch.id, Touch.payload["prompting"]).where(
                    Touch.payload["prompting"]["message_id"].astext == str(message_id)
                )
            )
        ).first()

    if row is None:
        return None

    stored = row[1] or {}
    return Answer(
        touch_id=int(row[0]),
        kind=str(stored.get("kind", "")),
        entry_ids=tuple(int(item) for item in stored.get("entry_ids", [])),
    )


__all__ = [
    "BLOCKER",
    "LOOK_BACK",
    "MIN_LENGTH",
    "QUIET_TOPIC",
    "RETURNING",
    "SET",
    "SILENCE_STREAK",
    "YESTERDAY",
    "Answer",
    "Item",
    "Question",
    "acceptable",
    "alone",
    "answered",
    "attach",
    "fits",
    "payload",
    "pick",
    "quote",
    "sample",
    "slots_from",
]
