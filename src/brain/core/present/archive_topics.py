"""Разговор по теме: несколько записей во времени, а не одна случайная.

Прямая претензия владельца 06.09.2026: «второй мозг, куда записываешь мысли.
Но я их все забыл, и он мне их не напомнит. Ты должен зайти, прочитать
и вспомнить — а он тебе даже этого не даёт». До этого дня `/recall <тема>`
отдавала **одну случайную** запись темы (`digest/by_topic.pick`), и по одной
записи не видно ничего: ни того, что владелец думал про эту тему месяц назад,
ни того, что он думает про неё сейчас.

Требования `vision.md` §8 («Диалог с архивом») выполняются здесь так:

    синтез + источники с датами   у каждой цитаты дата, и она же — якорь,
                                  по которому запись ищется целиком
    эволюция вместо выжимки       записи идут по времени и группируются
                                  по месяцам: «в августе так, в сентябре так»
    честность о пустоте           «касались дважды, оба раза вскользь» —
                                  полноценный ответ, а не «ничего не нашёл»

**Ни одного вызова модели.** Отбор, пороги, порядок, группировка и все
формулировки — код. Это минимальный режим Фазы 4 (`config.RECALL_WRITTEN`,
`HABIT_WRITTEN`, `PROMPTING_WRITTEN`): бесплатный тир даёт 20 запросов в сутки
на модель, и разговор по теме, который владелец заводит по несколько раз
в день, в них не влезает. Модельная ветка здесь **не написана**, а не выключена
флагом: флаг без кода за ним — обещание, которое некому выполнить.

**Приватные записи в разговор не попадают по построению, а не по фильтру.**
Тема появляется у записи из разметки, а разметка идёт через модель, куда
приватная запись не уходит (инвариант 5). Значит, у приватной записи тем нет
вовсе, и отдельное условие в запросе проверяло бы то, чего не бывает. Обратная
сторона названа вслух: приватную мысль этот механизм не вспомнит никогда.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from brain.core.digest.resurface import MIN_LENGTH
from brain.core.memory.repository import display_text
from brain.core.present import entry as entry_view
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry
from brain.storage.models.topic import EntryTopic, Topic, normalize

logger = logging.getLogger(__name__)

# Сколько записей показывать. Пять — потому что сообщение читается с телефона,
# а шестая цитата уже делает его лентой. Отбираются они **равномерно
# по времени**, а не последние пять: смысл разговора в движении, а последние
# пять — это срез одной недели.
MAX_MOMENTS = 5

# Насколько старой должна быть запись, чтобы попасть в разговор. Ноль — то есть
# порога нет вовсе.
#
# **Разведено с всплыванием 7 сентября 2026, решение владельца.** Прежде здесь
# стоял `by_topic.MIN_AGE` — сутки, унаследованные от всплывания, где они верны:
# всплывание достаёт старое **без повода**, и сегодняшнее владелец помнит без
# бота. Разговор по теме он заводит сам, и вопрос в нём ровно обратный —
# «а что сейчас». Довод владельца дословно: на вопрос «что там по учёбе»
# сегодняшняя запись самая нужная, а не самая лишняя.
#
# Константа не удалена, а оставлена нулём намеренно: она называет решение.
# Убрав её, мы получили бы модуль, в котором про возраст не сказано ничего,
# — и следующая правка вернула бы сюда чужой порог тем же путём, каким он сюда
# попал в первый раз.
MIN_AGE = timedelta(0)

# Сколько символов записи цитировать. Больше, чем во всплывании старого
# у сводки (70), и столько же, сколько у связи (200): здесь смысл именно
# в том, чтобы вспомнить мысль, а не узнать её в списке.
SNIPPET = 220

# Сколько букв должно совпасть, чтобы два слова считались одним. Нужно ради
# словоформ: владелец спрашивает «что там по учёбе», а тема называется
# «учеба» — и `normalize` их не сводит, потому что она про «ё», а не про
# падежи. Морфологии в проекте нет и не заводится ради этого зависимость
# (`pymorphy` — новая зависимость, то есть ADR).
#
# Правило простое: общее начало не короче четырёх букв, а хвосты с обеих
# сторон не длиннее трёх. «учебе»/«учеба» — общее «учеб», хвосты по одной
# букве. «работой»/«разработка» — общее «ра», две буквы, не совпадают.
# Слова короче четырёх букв сравниваются целиком: иначе «по» совпало бы
# с «похудение».
MIN_STEM = 4
MAX_TAIL = 3

# Что считается «сказанным только что». К показу отношения не имеет — нужно
# ровно одному месту: честному ответу, когда цитировать нечего (`_nothing_
# to_tell`). Отдельным именем, а не `by_topic.MIN_AGE`, потому что это другой
# вопрос: не «стоит ли показывать», а «помнит ли владелец это сам».
FRESH = timedelta(days=1)

MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


@dataclass(frozen=True, slots=True)
class Moment:
    """Одна запись темы: когда сказана и что в ней."""

    entry_id: int
    text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Talk:
    """Что есть у архива по теме.

    `total` считает **все** записи темы, включая те, что цитировать нечем:
    короткие и сегодняшние. Без него ответ «рассказывать нечего» неотличим
    от «такой темы нет», а это разные вещи и разные ответы (`vision.md` §8,
    требование 3).
    """

    topic: str
    total: int
    # Сколько записей темы моложе суток. Нужно ровно затем, чтобы не соврать
    # в ответе про пустоту: «касались дважды, оба раза вскользь» — правда про
    # два обрывка и неправда про две сегодняшние развёрнутые записи.
    fresh: int
    moments: list[Moment]


async def collect(name: str, now: datetime) -> Talk | None:
    """Что архив помнит по теме. `None` — такой темы в архиве нет вовсе.

    Тема ищется сначала точным `slug`, потом по общему началу слов: владелец
    набирает «что там по учёбе», а тема в базе называется «учеба».
    """
    found = await _match(name)
    if found is None:
        return None

    topic, slug = found
    body = display_text()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, body, Entry.created_at)
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .join(Entry, Entry.id == EntryTopic.source_entry_id)
                .where(Topic.slug == slug, Entry.created_at <= now)
                .order_by(Entry.created_at, Entry.id)
            )
        ).all()

    # Два порога, и они пришли из **разных** мест намеренно.
    #
    #   возраст   свой, и он нулевой — см. `MIN_AGE` выше. Разведён
    #             с всплыванием 07.09.2026 решением владельца: «что там
    #             по учёбе» спрашивают про сейчас, а не про позапрошлый месяц;
    #   длина     общий с всплыванием (`resurface.MIN_LENGTH`), и общий
    #             по существу: «напомни в 14:50 пойти домой» вспоминать нечего
    #             ни там, ни здесь.
    moments = [
        Moment(entry_id=int(row[0]), text=str(row[1]), created_at=row[2])
        for row in rows
        if row[1] is not None
        and row[2] <= now - MIN_AGE
        and len(" ".join(str(row[1]).split())) >= MIN_LENGTH
    ]

    # Сколько записей темы моложе суток. Показу они больше не мешают, но
    # в ответе про пустоту нужны по-прежнему: «касались дважды, оба раза
    # вскользь» — неправда про две сегодняшние развёрнутые записи. Сутки здесь
    # не порог показа, а срок, за который владелец помнит сказанное сам.
    fresh = sum(1 for row in rows if row[2] > now - FRESH)

    logger.info(
        "разговор по теме %r: записей %s, из них моложе суток %s, годных к показу %s",
        topic,
        len(rows),
        fresh,
        len(moments),
    )
    return Talk(topic=topic, total=len(rows), fresh=fresh, moments=moments)


async def _match(name: str) -> tuple[str, str] | None:
    """Имя темы в базе и её `slug`. `None` — не нашлось ни точно, ни по началу.

    Берутся только темы, у которых есть записи: базовый список заводится
    миграцией (ADR-0011) и содержит темы, которых владелец не касался ни разу,
    — предлагать разговор по пустой теме не о чем.
    """
    asked = normalize(name)
    if not asked:
        return None

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Topic.name, Topic.slug, func.count().label("entries"))
                .select_from(EntryTopic)
                .join(Topic, Topic.id == EntryTopic.topic_id)
                .group_by(Topic.name, Topic.slug)
                .order_by(func.count().desc(), Topic.slug)
            )
        ).all()

    for row in rows:
        if str(row[1]) == asked:
            return str(row[0]), str(row[1])

    # По общему началу слов. Порядок строк уже по убыванию числа записей,
    # поэтому при равном счёте выигрывает тема, к которой владелец возвращался
    # чаще, — а не та, что оказалась первой по алфавиту.
    best: tuple[str, str] | None = None
    best_score = 0
    for row in rows:
        score = _score(asked.split(), str(row[1]).split())
        if score > best_score:
            best, best_score = (str(row[0]), str(row[1])), score

    if best is not None:
        logger.info("тема %r опознана по словоформе как %r", name, best[0])
    return best


def _score(asked: list[str], slug: list[str]) -> int:
    """Сколько слов темы нашлось в спрошенном.

    Ноль означает «тема не та». Цена правила названа вслух: слово запроса,
    случайно совпавшее началом с именем темы, уведёт вопрос в разговор по теме
    вместо поиска по архиву. Стоит это одного лишнего сообщения — запись
    владельца при этом уже сохранена, а поиск он может позвать `/find`.
    """
    return sum(1 for word in slug if any(_same_word(word, other) for other in asked))


def _same_word(one: str, other: str) -> bool:
    """Одно ли это слово в разных падежах. См. `MIN_STEM`."""
    if len(one) < MIN_STEM or len(other) < MIN_STEM:
        return one == other

    shared = 0
    for left, right in zip(one, other, strict=False):
        if left != right:
            break
        shared += 1

    return shared >= MIN_STEM and len(one) - shared <= MAX_TAIL and len(other) - shared <= MAX_TAIL


def text(talk: Talk, *, timezone: str, now: datetime) -> str:
    """Разговор по теме словами. Всё до единого слова — код.

    `now` приходит параметром, а не берётся часами: год у даты пишется только
    у прошлогодней записи, и это решение зависит от «сегодня». Функция, которая
    смотрит на часы сама, проверяется только тестом с прибитой датой — а такой
    тест зелен ровно до того дня, когда часы его обгонят.
    """
    if not talk.moments:
        return _nothing_to_tell(talk)

    shown = _spread(talk.moments)
    zone = ZoneInfo(timezone)
    lines = [f"🧭 {talk.topic} — {_entries(talk.total)}, {_span(shown, zone=zone, now=now)}."]

    # Заголовок месяца — и есть то самое «в августе писал так, в сентябре так».
    # Ставится только когда месяцев больше одного: над списком из трёх
    # августовских записей он не сообщает ничего.
    by_months = _months(shown, zone=zone) > 1
    month: tuple[int, int] | None = None

    for moment in shown:
        when = moment.created_at.astimezone(zone)
        if by_months and (when.year, when.month) != month:
            lines += ["", f"{_month_name(when, now=now)}:"]
        elif month is None:
            lines.append("")
        month = (when.year, when.month)
        # Якорь — за цитатой и отбитый от неё. Здесь он нужнее всего:
        # предел цитаты 220 символов, и это то самое место, про которое
        # владелец сказал «то же „и ещё 64“, только в другом месте».
        lines.append(
            f"{_stamp(when, now=now)} · {_snippet(moment.text)}  "
            f"{entry_view.anchor(moment.entry_id)}"
        )

    hidden = talk.total - len(shown)
    if hidden > 0:
        lines += ["", f"Ещё {_entries(hidden)} по этой теме: /find {talk.topic}"]

    return "\n".join(lines)


def _nothing_to_tell(talk: Talk) -> str:
    """Честный ответ, когда цитировать нечего (`vision.md` §8, требование 3).

    «Касался дважды, оба раза вскользь» — полноценный ответ, а «ничего
    не нашёл» на том же месте — вранье: записи есть, просто рассказывать
    по ним нечего.

    **Причина названа та, которая есть.** «Вскользь» про две сегодняшние
    развёрнутые записи было бы такой же неправдой, как «ничего не нашёл»:
    там нечего вспоминать не потому, что коротко, а потому, что владелец
    сказал это сегодня и помнит без бота.
    """
    if talk.fresh == talk.total:
        return (
            f"🧭 {talk.topic} — {_entries(talk.total)} за последние сутки.\n\n"
            "Вспоминать пока нечего: сегодняшнее вы помните и без меня."
        )

    touched = {
        1: "касались один раз, и то вскользь",
        2: "касались дважды, оба раза вскользь",
    }.get(talk.total, f"касались {talk.total} раз, и каждый раз вскользь")
    return (
        f"🧭 {talk.topic} — {touched}.\n\n"
        "Развёрнутых записей старше суток по этой теме нет: всё либо "
        "в пару слов, либо сказано сегодня."
    )


def _spread(moments: list[Moment]) -> list[Moment]:
    """Равномерно по времени, обязательно с первой и последней.

    Последние пять показали бы одну неделю, а разговор про тему — про
    движение. Первая и последняя записи в выдаче есть всегда: между ними
    и видно, что изменилось.
    """
    if len(moments) <= MAX_MOMENTS:
        return moments
    step = (len(moments) - 1) / (MAX_MOMENTS - 1)
    return [moments[round(number * step)] for number in range(MAX_MOMENTS)]


def _span(moments: list[Moment], *, zone: ZoneInfo, now: datetime) -> str:
    """Отрезок времени, который покрывает разговор."""
    first = moments[0].created_at.astimezone(zone)
    last = moments[-1].created_at.astimezone(zone)
    if first.date() == last.date():
        return f"всё за {_stamp(first, now=now)}"
    return f"с {_stamp(first, now=now)} по {_stamp(last, now=now)}"


def _months(moments: list[Moment], *, zone: ZoneInfo) -> int:
    """Сколько разных месяцев покрывают показанные записи."""
    return len(
        {(m.created_at.astimezone(zone).year, m.created_at.astimezone(zone).month) for m in moments}
    )


def _month_name(when: datetime, *, now: datetime) -> str:
    """«Август» или «Август 2025» — год у прошлогоднего месяца обязателен."""
    name = MONTHS[when.month - 1].capitalize()
    return name if when.year == now.year else f"{name} {when.year}"


def _stamp(when: datetime, *, now: datetime) -> str:
    """Дата строки. Год пишется только у прошлогодней записи — как в поиске."""
    return f"{when:%d.%m}" if when.year == now.year else f"{when:%d.%m.%Y}"


def _entries(count: int) -> str:
    """«1 запись», «2 записи», «5 записей» — по-русски, а не «записей: 1»."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} записей"
    last = count % 10
    if last == 1:
        return f"{count} запись"
    if 2 <= last <= 4:
        return f"{count} записи"
    return f"{count} записей"


def _snippet(said: str) -> str:
    """Кусок записи одной строкой.

    Своя, а не `digest.render.snippet`: там она про строку сводки и обрезает
    по 70 символов, здесь — про цитату, которую владелец читает. Одна функция
    на два разных предела означала бы, что предел приезжает аргументом
    из каждого места вызова, а он и есть решение модуля.
    """
    flat = " ".join(said.split())
    if len(flat) <= SNIPPET:
        return flat
    return flat[: SNIPPET - 1].rstrip() + "…"
