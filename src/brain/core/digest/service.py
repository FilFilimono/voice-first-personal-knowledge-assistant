"""Тик касаний: что сегодня уместно сказать, а что нет.

Зовётся расписанием раз в пять минут. Расписание здесь — только повод
посмотреть; решение принимается по данным, ровно как у переобобщения тем.
Почему не `cron 0 9 * * *` — ADR-0013, ось 2; коротко, там три отказа сразу:
перевод часов, простой воркера и двойное срабатывание при перезапуске.

**Порядок внутри слота обратный ожидаемому: сначала занять место в журнале,
потом отправлять.** Отправив первым, при гонке двух тиков мы получили бы два
одинаковых сообщения и одну строку журнала. Цена обратного порядка названа
вслух ниже: упавшая отправка оставляет слот занятым, и сводка в этот день
не придёт.

**Долг фиксируется на каждом тике и молча** — он не слот и в журнал касаний
не попадает вовсе, потому что касанием не является (`vision.md` §7.0, принцип 2).
"""

import logging
from datetime import date, datetime

from brain.core.digest import (
    commitments,
    evening,
    loops,
    morning,
    quiet,
    render,
    resurface,
    slots,
    touches,
    weekly,
)
from brain.core.ports import LanguageModel, Notifier
from brain.core.present import digest as lead
from brain.core.present import guard
from brain.storage.models.touch import SilenceReason, TouchCategory, TouchSlot

logger = logging.getLogger(__name__)

# Сколько спорных записей показывать за вечер. `vision.md` §7 обещает, что
# вечерний разбор занимает около двух минут; пачка на десять записей нарушает
# это обещание молча. Остальные подождут завтрашнего вечера, а через семь дней
# зафиксируются сами.
EVENING_BATCH = 3


async def tick(
    *,
    now: datetime,
    timezone: str,
    chat_id: int,
    notifier: Notifier,
    model: LanguageModel | None,
) -> list[TouchSlot]:
    """Один проход. Возвращает слоты, по которым что-то ушло.

    Ничего не отправляет, если отправлять нечего: это основной режим на молодом
    архиве, а не редкий угол (ADR-0013).

    `model` пишет лид утренней сводки (ADR-0015, п. 5). Аргумент обязателен
    и без умолчания намеренно: умолчание `None` означало бы, что забытый
    параметр в воркере выглядит как работающая сводка — ровно тот почерк,
    которым в проекте уже потерялась `loops.is_stuck`. `None` передаётся
    осознанно и значит «сухой рендер»: так тик зовут тесты, которые про
    расписание, а не про текст.
    """
    # Долг — первым и всегда: он не зависит от слотов, от паузы и от бюджета,
    # потому что не является сообщением.
    await evening.settle_debt(now)

    day = slots.local_date(now, timezone)
    done = await touches.of_day(day)
    silent = await quiet.is_quiet(now)

    sent: list[TouchSlot] = []
    for slot in slots.due(now, timezone):
        if slot in done:
            continue
        if await _handle(slot, day, now, timezone, chat_id, notifier, silent=silent, model=model):
            sent.append(slot)
    return sent


async def _handle(
    slot: TouchSlot,
    day: date,
    now: datetime,
    timezone: str,
    chat_id: int,
    notifier: Notifier,
    *,
    silent: bool,
    model: LanguageModel | None,
) -> bool:
    """Разобраться с одним слотом. `True` — сообщение ушло."""
    if silent:
        # Пауза глушит инициативные касания **и утреннюю сводку** (`vision.md`
        # §7). Напоминания сюда не заходят вовсе — они идут своим путём.
        await touches.record_silence(slot, day, reason=SilenceReason.QUIET)
        return False

    # Бюджет только для инициативных: утренняя сводка вне бюджета, она про
    # сегодняшний день, а не про архив (`vision.md` §7).
    initiative = touches.CATEGORIES[slot] is TouchCategory.INITIATIVE
    if initiative and (await touches.budget(day)).exhausted:
        await touches.record_silence(slot, day, reason=SilenceReason.OVER_BUDGET)
        return False

    prepared = await _prepare(slot, now, timezone, model)
    if prepared is None:
        await touches.record_silence(slot, day, reason=SilenceReason.NOTHING_TO_SAY)
        return False

    text, payload = prepared

    # Сначала занять слот, потом отправлять — см. модуль.
    if not await touches.record_sent(slot, day, payload=payload):
        logger.info("слот %s за %s уже занят соседним тиком", slot, day)
        return False

    try:
        await _send(slot, chat_id, text, payload, notifier)
    except Exception:
        # Слот остаётся занятым намеренно. Повтор отправки рискует прислать
        # то же самое дважды, а пропущенная сводка стоит одного дня — завтра
        # придёт следующая. Что воркер жив, видно по пульсу в `/status`.
        logger.exception("касание %s не отправилось; слот за %s остаётся занятым", slot, day)
        return False

    await _after(slot, payload, now)
    return True


async def _prepare(
    slot: TouchSlot, now: datetime, timezone: str, model: LanguageModel | None
) -> tuple[str, dict[str, object]] | None:
    """Текст и нагрузка касания. `None` — сказать нечего.

    **Модель зовётся здесь, то есть до того, как слот занят.** Порядок ровно
    тот же, что был: сначала собрать текст, потом занять место в журнале,
    потом отправить. Цена — при гонке двух тиков лид напишется дважды, а слот
    достанется одному; переставлять из-за этого нельзя, потому что занятый слот
    при упавшей сборке стоит целого дня молчания, а лишний вызов — одного
    запроса из двадцати. Гонка при этом умозрительная: воркер один и тикает
    раз в пять минут.
    """
    if slot is TouchSlot.MORNING:
        content = await morning.collect(now)
        if content.empty:
            return None
        return render.morning(content, written=await _lead(model, content, now, timezone)), {
            "commitment_ids": [item.id for item in content.commitments],
            "loop_id": content.loop.id if content.loop else None,
            # Правило про три откладывания — часть замысла, а не оформления
            # (`vision.md` §5.2), поэтому признак считается в ядре, а не
            # в адаптере. Считает его сам `Morning`: тот же признак выбирает
            # вопрос в тексте сводки, и посчитанный тут второй раз он мог бы
            # разойтись с текстом молча. В журнал попадает вместе с остальной
            # нагрузкой: по строке касания должно быть видно, какой вопрос ушёл.
            "loop_stuck": content.loop_stuck,
        }

    if slot is TouchSlot.DAY:
        old = await resurface.pick(now)
        if old is None:
            return None
        return render.resurfaced(old, timezone=timezone), {"entry_id": old.entry_id}

    if slot is TouchSlot.EVENING:
        # Показанное вчера сегодня не повторяется. Не ответили — запись
        # зафиксируется сама через семь дней, молча (`vision.md` §7). Найдено
        # повтором по живому архиву: без этого одна и та же пачка из трёх
        # записей уходила бы семь вечеров подряд.
        disputed = await evening.disputed(
            limit=EVENING_BATCH, exclude=await touches.shown_in_evening()
        )
        if not disputed:
            return None
        return render.evening(disputed), {"entry_ids": [item.entry_id for item in disputed]}

    review = await weekly.collect(now)
    if review is None:
        return None
    return render.weekly(review), {}


async def _lead(
    model: LanguageModel | None, content: morning.Morning, now: datetime, timezone: str
) -> guard.Written | None:
    """Лид сводки от модели. `None` — сводка уйдёт сухим рендером.

    Отбор уже сделан: в модель уходит ровно то, что владелец увидит, — те же
    три дела и та же петля. Возраст и состояние дела прикладываются, потому что
    без них лид получится пересказом количества; **счётчик откладываний петли
    не прикладывается**, и это решение владельца — см. `core/present/digest`.

    **Бюджет считает код.** Сухие строки уже написаны, и сколько слов осталось
    лиду, знает только тот, кто их поставил: сорок секунд из `vision.md` §5.1 —
    это про всё сообщение, а не про то, что написала модель. Сухой рендер
    собирается для счёта дважды — он чистая функция, и второй вызов стоит
    склейки десятка строк.
    """
    if model is None:
        return None

    return await lead.write(
        model,
        deeds=[
            lead.Deed(
                id=item.id,
                # То же, что увидит владелец: название, а если его нет —
                # снимок единицы. Владельцу оно достанется подрезанным
                # по ширине строки, модели — целиком.
                text=item.title or item.text,
                created_at=item.created_at,
                state=item.state,
            )
            for item in content.commitments
        ],
        question=(
            None
            if content.loop is None
            else lead.Question(id=content.loop.id, text=content.loop.title or content.loop.question)
        ),
        now=now,
        timezone=timezone,
        words=lead.WORD_BUDGET - len(render.morning(content).split()),
    )


async def _send(
    slot: TouchSlot,
    chat_id: int,
    text: str,
    payload: dict[str, object],
    notifier: Notifier,
) -> None:
    """Отправить — тем способом, который подходит слоту."""
    if slot is TouchSlot.MORNING:
        loop_id = payload.get("loop_id")
        await notifier.send_morning(
            chat_id,
            text,
            commitment_ids=[int(item) for item in _ids(payload, "commitment_ids")],
            loop_id=int(loop_id) if isinstance(loop_id, int) else None,
            loop_stuck=payload.get("loop_stuck") is True,
        )
        return

    if slot is TouchSlot.EVENING:
        await notifier.send_evening(
            chat_id, text, entry_ids=[int(item) for item in _ids(payload, "entry_ids")]
        )
        return

    # Всплывание и ревью — беззвучно и без кнопок (`vision.md` §7).
    await notifier.send_quietly(chat_id, text)


def _ids(payload: dict[str, object], key: str) -> list[int]:
    value = payload.get(key)
    return [int(item) for item in value] if isinstance(value, list) else []


async def _after(slot: TouchSlot, payload: dict[str, object], now: datetime) -> None:
    """Что надо отметить уже после того, как сообщение ушло.

    Только петля: «спросили» отмечается после отправки, потому что промежуток
    в две недели должен отсчитываться от настоящего вопроса, а не от намерения
    его задать.
    """
    if slot is not TouchSlot.MORNING:
        return
    loop_id = payload.get("loop_id")
    if isinstance(loop_id, int):
        await loops.mark_asked(loop_id, now=now)


async def counts(now: datetime) -> tuple[int, int]:
    """Сколько дел и петель висит. Нужно `/status`.

    Момент нужен делам: отложенное до срока не висит, и число в `/status`
    обязано совпадать с тем, что в это же утро показала сводка.
    """
    return await commitments.open_count(now), await loops.open_count()
