"""Привычки: то, что делается регулярно, и напоминает о себе само.

`vision.md` §5.11. Три свойства оттуда, без которых модуль читается неверно:

- **привычка не висит делом и не копится.** В список незакрытых обещаний
  она не попадает вовсе — не показом, а устройством: дела заводятся только
  из единиц типа `task` (`commitments.FROM_TYPE`), и `habit` туда не входит;
- **не выполненная вчера — не долг.** Ни счётчика пропусков, ни серии.
  Пропущенная привычка просто наступает снова, в свой срок, и вчерашнее
  не упоминается — иначе это ретроспективный упрёк (§7.0, принципы 2 и 5);
- **вопрос — про ход, а не про факт.** Не «сделал или нет»: на такой вопрос
  отвечают кнопкой, а спрашивается продвижение.

**Заводится с подтверждения, как петля** (§5.2). Ритм предлагает код, разбирая
слова записи, но нажимает владелец: механизм, который начинает напоминать сам,
слишком дорого стоит при ошибке распознавания. Молча заведённая привычка —
это ежедневное сообщение, о котором не просили.

**Напоминание — заказанное касание вне бюджета, но беззвучное.** Единственное
исключение из правила «заказанные — со звуком» (§7.1): заказанное со звуком —
это встреча через десять минут, а привычка приходит каждый день, и ежедневный
звук её обесценит.

**Контекст напоминания считается связями** (`core/retrieval/links.py`, блок C
той же фазы): §5.11 требует, чтобы вместе с напоминанием показывалось то, что
владелец **сам говорил по этой теме**, а не одно название. Голое «почитай
книгу» — будильник, а от будильника отписываются. Связи считаются векторной
близостью, без вызова модели (инвариант 8), и берутся они от **записи, из
которой привычка заведена**: она и есть формулировка темы словами владельца.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.config import get_settings
from brain.core.digest import render
from brain.core.memory.repository import working_text
from brain.core.ports import Notifier
from brain.core.retrieval import links
from brain.core.scheduler import recurrence
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationType
from brain.storage.models.entry import Entry
from brain.storage.models.habit import Habit, HabitStatus, Rhythm
from brain.storage.models.unit import EntryUnit

logger = logging.getLogger(__name__)

# Насколько поздно ещё уместно спросить «как идёт». Воркер может лежать полдня;
# поднявшись в полночь, он не должен спрашивать про день, которого уже нет, —
# та же мысль, что у окон слотов (`digest/slots.py`). Пропущенное напоминание
# при этом **не досылается никогда**: привычка наступает снова, в свой срок.
LATE = timedelta(hours=6)

# Час, в который спрашивать, если время суток в записи не названо.
# Значение и объяснение — `config.habit_hour`; здесь константы больше нет,
# чтобы час не оказался записан в двух местах и не разошёлся молча.


async def create(
    *,
    entry_id: int,
    unit_id: int | None,
    text: str,
    rhythm: Rhythm,
    days: tuple[int, ...],
    times_per_week: int | None,
    hour: int,
    minute: int = 0,
) -> int:
    """Завести привычку по подтверждению владельца. Повторно — не заводит.

    Кнопка живёт в переписке вечно, нажать её можно сколько угодно раз, и второе
    нажатие не должно завести второе ежедневное сообщение. Держит это
    уникальность по записи, а не проверка в коде: проверка «сначала посмотрим,
    потом вставим» пропустила бы оба нажатия, пришедших разом.
    """
    async with session_scope() as session:
        created = await session.scalar(
            pg_insert(Habit)
            .values(
                source_entry_id=entry_id,
                source_unit_id=unit_id,
                text=text.strip(),
                rhythm=rhythm,
                days=list(days) or None,
                times_per_week=times_per_week,
                hour=hour,
                minute=minute,
                status=HabitStatus.ACTIVE,
            )
            .on_conflict_do_nothing(index_elements=[Habit.source_entry_id])
            .returning(Habit.id)
        )
        if created is not None:
            return int(created)

        existing = await session.scalar(select(Habit.id).where(Habit.source_entry_id == entry_id))

    if existing is None:  # pragma: no cover — вставка не прошла и строки нет
        raise RuntimeError("привычка не завелась")
    return int(existing)


@dataclass(frozen=True, slots=True)
class Proposal:
    """Что система предлагает завести — до того, как владелец нажал кнопку.

    Ритм здесь **может отсутствовать**, и это не ошибка разбора, а рабочее
    состояние: `vision.md` §5.11 велит в таком случае спросить, а не угадать.
    Умолчания у ритма нет намеренно — угаданный ритм это ежедневное сообщение,
    выбранное за владельца, и ошибка в нём обнаружится не разговором,
    а раздражением.
    """

    entry_id: int
    unit_id: int
    # Одно предложение, а не вся единица: текст показывается каждый день,
    # и соседняя фраза уехала бы туда вместе с ним (`recurrence.clause`).
    text: str
    rhythm: recurrence.Recurrence | None
    hour: int


async def proposal(entry_id: int) -> Proposal | None:
    """Что предложить владельцу под разбором. `None` — предлагать нечего.

    `None` означает одно из трёх, и все три законны: разметка не нашла в записи
    привычки; привычка по этой записи уже заведена (второй раз не предлагаем —
    кнопка живёт в переписке вечно); запись переразметили между показом кнопки
    и нажатием.

    Текст берётся из **единицы**, а не из записи целиком, — та же причина, что
    у петли (`loops.create_from_entry`): запись «каждый день читать, а ещё надо
    купить хлеб» содержит привычку только в первой половине.
    """
    if await by_entry(entry_id) is not None:
        return None

    async with session_scope() as session:
        latest = await session.scalar(
            select(func.max(Annotation.id)).where(Annotation.source_entry_id == entry_id)
        )
        if latest is None:
            return None

        found = (
            await session.execute(
                select(EntryUnit.id, EntryUnit.span_start, EntryUnit.span_end)
                .where(EntryUnit.annotation_id == latest, EntryUnit.type == AnnotationType.HABIT)
                .order_by(EntryUnit.ordinal)
                .limit(1)
            )
        ).first()
        if found is None:
            return None

        unit_id, start, end = found
        # Рабочий текст, а не вычищенный: отрезки единиц посчитаны над ним
        # (ADR-0015, ось 3), и вычищенный резался бы по чужим границам.
        body = await session.scalar(select(working_text()).where(Entry.id == entry_id))

    unit = str(body or "")[start:end].strip()
    if not unit:
        return None

    return Proposal(
        entry_id=entry_id,
        unit_id=int(unit_id),
        text=recurrence.clause(unit),
        rhythm=recurrence.parse(unit),
        hour=recurrence.hour_of(unit, get_settings().habit_hour),
    )


async def confirm(
    offered: Proposal, *, chosen: Rhythm | None, now: datetime, timezone: str
) -> int | None:
    """Завести привычку по нажатию. `None` — ритм неизвестен, надо спросить.

    Ритм приходит одним из двух путей и никогда третьим: он либо разобран
    из слов записи (`Proposal.rhythm`), либо назван кнопкой (`chosen`).
    Нажатие перебивает разбор — владелец видел предложенный ритм и выбрал
    другой, значит разбор ошибся.

    Дни недели у «N раз в неделю» считаются от **дня подтверждения**: сколько
    раз, сказал владелец, а в какие именно дни — не сказал, и спрашивать это
    после «два раза в неделю» значило бы требовать точности, которой в замысле
    не было (`recurrence.spread`).
    """
    found = offered.rhythm if chosen is None else recurrence.chosen(chosen)
    if found is None:
        return None

    weekday = now.astimezone(ZoneInfo(timezone)).weekday()
    return await create(
        entry_id=offered.entry_id,
        unit_id=offered.unit_id,
        text=offered.text,
        rhythm=found.rhythm,
        days=recurrence.days_of(found, anchor_weekday=weekday),
        times_per_week=found.times_per_week,
        hour=offered.hour,
    )


async def by_entry(entry_id: int) -> Habit | None:
    """Привычка этой записи. Нужна показу и повторному нажатию кнопки."""
    async with session_scope() as session:
        found: Habit | None = await session.scalar(
            select(Habit).where(Habit.source_entry_id == entry_id)
        )
    return found


async def active() -> list[Habit]:
    """Живые привычки. Остановленные не спрашиваются никогда."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(Habit).where(Habit.status == HabitStatus.ACTIVE).order_by(Habit.id)
        )
    return list(rows.all())


def is_due(habit: Habit, *, now: datetime, timezone: str) -> bool:
    """Пора ли спросить про эту привычку прямо сейчас.

    Решение принимается **по данным**, а не по расписанию: тик приходит раз
    в пять минут и каждый раз спрашивает заново, наступил ли день и прошёл ли
    час. Фиксированное задание на «каждый день в 9» ломалось бы на переводе
    часов, на простое воркера и на перезапуске — ровно то, из-за чего в проекте
    нет ни одного `cron 0 9 * * *` (ADR-0013, ось 2).
    """
    local = now.astimezone(ZoneInfo(timezone))
    day = local.date()

    if not recurrence.occurs_on(
        day,
        rhythm=habit.rhythm,
        days=habit.days or (),
        anchor=habit.created_at.astimezone(ZoneInfo(timezone)).date(),
    ):
        return False

    due_at = local.replace(hour=habit.hour, minute=habit.minute, second=0, microsecond=0)
    if local < due_at or local - due_at > LATE:
        return False

    # Сегодня уже спрашивали — значит больше не спрашиваем. Это не счёт
    # выполненного и не серия: единственное, от чего защищает эта строка, —
    # тик, приходящий каждые пять минут. Без неё окно в шесть часов дало бы
    # семьдесят два одинаковых сообщения за одно утро.
    #
    # Сравнение по **дате владельца**, а не по промежутку: «сегодня уже» —
    # это его сутки, и напоминание в 9 утра после вчерашнего в 23:50 обязано
    # прийти, хотя между ними меньше суток.
    if habit.last_reminded_at is not None:
        return habit.last_reminded_at.astimezone(ZoneInfo(timezone)).date() < day
    return True


async def mark_reminded(habit_id: int, *, now: datetime) -> bool:
    """Отметить, что сегодня спросили. `False` — соседний тик успел раньше.

    Отметка ставится **до отправки** и одним запросом с проверкой прежнего
    значения: два одинаковых вопроса за одно утро раздражают сильнее, чем
    отсутствие одного. Тот же порядок, что у слотов касаний, и по той же
    причине (`digest/service.py`).
    """
    async with session_scope() as session:
        changed = await session.scalar(
            update(Habit)
            .where(
                Habit.id == habit_id,
                (Habit.last_reminded_at.is_(None)) | (Habit.last_reminded_at < now),
            )
            .values(last_reminded_at=now)
            .returning(Habit.id)
        )
    return changed is not None


async def context_of(habit: Habit) -> list[links.Link]:
    """То, что владелец сам говорил по теме привычки. Пусто — не говорил.

    Считается **векторной близостью, без вызова модели** (инвариант 8), от той
    записи, из которой привычка заведена: она и есть формулировка темы его
    собственными словами. Правила отбора — общие со связями в момент записи
    (`vision.md` §5.4): один порог, один зазор по возрасту, одно понимание
    слова «похоже». Второй набор чисел рядом означал бы, что «перекликается»
    значит в системе разное в разных местах.

    **Настройка `LINKS_ENABLED` здесь не спрашивается**, и это не недосмотр.
    Она выключает сообщение «а кстати…», приходящее на **каждую** запись;
    напоминание привычки владелец подтвердил сам, и молчать о заказанном
    система не имеет права (`vision.md` §7.1). Выключенные связи должны
    оставить его без контекста, а не без напоминания.

    Отказ здесь ничего не роняет: без контекста напоминание беднее, но оно
    всё ещё напоминание, а исключение отсюда стоило бы целого дня привычки.
    """
    try:
        return await links.related(habit.source_entry_id, rules=links.from_settings(get_settings()))
    except Exception:
        logger.exception("контекст привычки %s не посчитался", habit.id)
        return []


async def remind_due(
    *, now: datetime, timezone: str, chat_id: int, notifier: Notifier
) -> list[int]:
    """Спросить про все привычки, чей срок наступил. Вернуть их номера.

    **Вне бюджета и вне слотов** — это заказанное касание (§7.1): владелец
    подтвердил его сам, и система не имеет права молчать о том, о чём её
    просили. По той же причине пауза его не глушит: `/pause` глушит
    инициативные касания и утреннюю сводку, а не то, что владелец поставил сам.

    **Беззвучно** — единственное исключение из правила «заказанные со звуком».

    Молчание пишется в лог: тишина системы и её смерть снаружи неотличимы.
    """
    reminded: list[int] = []
    for habit in await active():
        if not is_due(habit, now=now, timezone=timezone):
            continue
        if not await mark_reminded(habit.id, now=now):
            logger.info("привычка %s: про неё уже спросил соседний тик", habit.id)
            continue

        try:
            await notifier.send_quietly(
                chat_id, render.habit(habit, await context_of(habit), timezone=timezone)
            )
        except Exception:
            # Отметка остаётся: повтор отправки рискует прислать то же самое
            # дважды, а пропущенный день привычки стоит одного дня — завтра
            # она наступит снова. То же решение, что у слотов касаний.
            logger.exception("напоминание привычки %s не отправилось", habit.id)
            continue

        reminded.append(habit.id)

    if reminded:
        logger.info("привычки напомнили: %s", ", ".join(str(item) for item in reminded))
    return reminded


async def open_count() -> int:
    """Сколько привычек живо. Нужно `/status`: механизм, который сам шлёт
    сообщения, обязан быть виден оттуда, а не только из базы."""
    return len(await active())
