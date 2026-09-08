"""Тик касаний: что сегодня уместно сказать, а что нет.

Зовётся расписанием раз в пять минут. Расписание здесь — только повод
посмотреть; решение принимается по данным, ровно как у переобобщения тем.
Почему не `cron 0 9 * * *` — ADR-0013, ось 2; коротко, там три отказа сразу:
перевод часов, простой воркера и двойное срабатывание при перезапуске.

**Порядок внутри слота обратный ожидаемому: сначала занять место в журнале,
потом отправлять.** Отправив первым, при гонке двух тиков мы получили бы два
одинаковых сообщения и одну строку журнала.

**Каждый механизм тика огорожен, и это правка 7 сентября 2026.** До неё порядок
«долг → привычки → слоты» шёл без единого `try`, и упавшие привычки унесли
с собой утреннюю сводку, раскачку, всплывание, вечерний разбор — **и запись
молчания в журнал**. Снаружи это неотличимо от «сегодня нечего сказать».
Теперь упавший механизм пишет строку в `tick_failures`, упавший слот получает
`silence_reason = failed`, а остальные идут дальше. Строка `failed` **не сжигает
сутки**: следующий тик через пять минут пробует снова и переписывает её.

**Долг фиксируется на каждом тике и молча** — он не слот и в журнал касаний
не попадает вовсе, потому что касанием не является (`vision.md` §7.0, принцип 2).
"""

import logging
from collections.abc import Awaitable
from datetime import date, datetime

from brain.config import get_settings
from brain.core.digest import (
    commitments,
    evening,
    event_context,
    failures,
    habits,
    loops,
    morning,
    prompting,
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
from brain.storage.models.touch import SilenceReason, Touch, TouchCategory, TouchSlot

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

    `model` пишет лид утренней сводки (ADR-0015, п. 5) и фразу перед встречей
    (ADR-0016, за флагом `EVENT_CONTEXT_WRITTEN`). Аргумент обязателен
    и без умолчания намеренно: умолчание `None` означало бы, что забытый
    параметр в воркере выглядит как работающая сводка — ровно тот почерк,
    которым в проекте уже потерялась `loops.is_stuck`. `None` передаётся
    осознанно и значит «сухой рендер»: так тик зовут тесты, которые про
    расписание, а не про текст.
    """
    day = slots.local_date(now, timezone)

    # Долг — первым и всегда: он не зависит от слотов, от паузы и от бюджета,
    # потому что не является сообщением.
    await _isolated("фиксация долга", day, evening.settle_debt(now))

    # Привычки — до слотов и **вне** их. Это заказанное касание (`vision.md`
    # §7.1): владелец подтвердил его сам, и система не имеет права молчать
    # о том, о чём её просили. Отсюда три следствия, которые видно только тут:
    # напоминание не занимает слот, не тратит бюджет трёх инициативных
    # и не глушится паузой — `/pause` глушит инициативные касания и утреннюю
    # сводку, а не то, что владелец поставил себе сам.
    #
    # Зовётся отсюда, а не отдельным расписанием, по той же причине, по которой
    # тик вообще есть: решение принимается по данным, а «наступил ли срок»
    # спрашивается у привычки каждые пять минут (`habits.is_due`). Второе
    # расписание рядом означало бы второе место, где считается время владельца.
    #
    # Возвращённые номера здесь не нужны и в слоты не попадают: слот —
    # это место в журнале касаний, а заказанное в бюджете не участвует.
    await _isolated(
        "напоминания привычек",
        day,
        habits.remind_due(now=now, timezone=timezone, chat_id=chat_id, notifier=notifier),
    )

    # Контекст перед событием — рядом с привычками и по тем же трём причинам
    # (`vision.md` §5.7 и §7.1): это заказанное касание, оно вне слотов, вне
    # бюджета трёх инициативных и не глушится паузой. Отличие от привычки одно
    # и оно в звуке: контекст уходит **со звуком**, потому что встреча через
    # двадцать минут требует внимания сейчас, а привычка приходит каждый день.
    #
    # Отдельного расписания под него нет намеренно: тик уже идёт раз в пять
    # минут, а `tech_plan.md` §8 просит ровно «опрос раз в 5 минут». Второе
    # расписание рядом означало бы второе место, где считается время владельца.
    #
    # `_isolated` здесь обязателен: календарь ходит в сеть, и его отказ
    # не имеет права унести утреннюю сводку — ровно то, что 7 сентября 2026
    # сделали упавшие привычки.
    await _isolated(
        "контекст перед событием",
        day,
        event_context.deliver_due(
            now=now, timezone=timezone, chat_id=chat_id, notifier=notifier, model=model
        ),
    )

    done = await touches.of_day(day)
    silent = await quiet.is_quiet(now)

    sent: list[TouchSlot] = []
    for slot in slots.due(now, timezone):
        if _settled(done.get(slot)):
            continue
        try:
            if await _handle(
                slot, day, now, timezone, chat_id, notifier, silent=silent, model=model
            ):
                sent.append(slot)
        except Exception as failure:
            # Слот упал — остальные слоты обязаны отработать. Строка `failed`
            # ставится затем, чтобы «сегодня нечего сказать» и «сегодня
            # сломалось» перестали выглядеть одинаково, и переписывается
            # следующим тиком: сбой стоит пяти минут, а не суток.
            logger.exception("слот %s за %s не отработал", slot, day)
            await failures.record(f"слот «{_SLOT_NAMES[slot]}»", failure, day=day)
            await touches.record_failure(slot, day)
    return sent


# Имена слотов по-русски: они уезжают в `/status`, который читает владелец.
# Значения `TouchSlot` там читались бы как отладочный вывод.
_SLOT_NAMES: dict[TouchSlot, str] = {
    TouchSlot.MORNING: "утро",
    TouchSlot.DAY: "день",
    TouchSlot.EVENING: "вечер",
    TouchSlot.WEEKLY: "неделя",
}


def _settled(row: Touch | None) -> bool:
    """Разрешён ли слот за эти сутки по существу.

    Строка `failed` разрешением не является: она означает, что решить
    не удалось. Следующий тик обязан попробовать снова — иначе один отказ
    провайдера съедает целые сутки сводки.
    """
    return row is not None and row.silence_reason is not SilenceReason.FAILED


async def _isolated(mechanism: str, day: date, work: Awaitable[object]) -> None:
    """Выполнить механизм так, чтобы его падение не унесло весь тик.

    **Тут вся правка 7 сентября 2026 в одну строку.** До неё порядок внутри
    тика был «долг → привычки → слоты» без единого `try`, и упавшие привычки
    унесли с собой утреннюю сводку, раскачку, всплывание, вечерний разбор —
    **и запись молчания в журнал**. Снаружи это неотличимо от «сегодня нечего
    сказать»: журнал молчание пишет, но пишет его тот же тик, который упал.

    Долг и напоминания привычек слотами не являются (они вне бюджета и вне
    журнала касаний, `vision.md` §7.1), поэтому строки в `touches` им
    не положено — их отказ уходит в `tick_failures` и виден в `/status`.
    Проглотить его молча было бы хуже, чем упасть: тихо не работающий механизм
    в этом проекте уже стоил недели.
    """
    try:
        await work
    except Exception as failure:
        logger.exception("механизм %r не отработал", mechanism)
        await failures.record(mechanism, failure, day=day)


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

    # Отправка **не глушится здесь**, и это правка 7 сентября 2026.
    #
    # Прежде упавшая отправка ловилась на месте и слот оставался занятым
    # до конца суток. Довод был «повтор рискует прислать то же самое дважды»,
    # и цена ему казалась в один день. Живая база показала, чего это стоит
    # на самом деле: `sent_at` пишется **до** отправки, поэтому в журнале
    # упавшая отправка выглядит точно так же, как состоявшаяся, — 7 сентября
    # журнал говорил «сводка отправлена в 09:30», а владелец не получил ничего
    # и узнал об этом только потому, что спросил. Молчание снова стало
    # неотличимо от смерти, только теперь с подтверждением в базе.
    #
    # Поэтому отказ уходит наверх, тик ставит слоту `failed`, и следующий тик
    # через пять минут пробует снова. **Цена названа вслух:** если Telegram
    # сообщение принял, а ответ до нас не дошёл, владелец получит его дважды.
    # Это дешевле суток тишины и, в отличие от неё, заметно.
    message_id = await _send(slot, chat_id, text, payload, notifier)

    # Дальше сообщение **уже ушло**, и упасть отсюда наверх нельзя: слот
    # получил бы `failed`, а следующий тик прислал бы то же самое второй раз.
    # Поэтому здесь отказ глушится по-настоящему — в лог.
    try:
        await _after(slot, payload, now)
        # Идентификатор ушедшего сообщения дописывается в журнал **после**
        # отправки: до неё его не существует. По нему потом узнаётся, что
        # владелец отвечает именно на вопрос раскачки, — тем же приёмом, каким
        # ответ на «когда напомнить?» узнаётся по `ack_message_id` в
        # `entries.meta`. Заодно это единственный след доставки в базе: его
        # отсутствие у строки с раскачкой и выдало 7 сентября.
        if "prompting" in payload:
            await touches.remember_message(slot, day, message_id=message_id)
    except Exception:
        logger.exception("касание %s ушло, но отметить его не удалось", slot)
    return True


async def _prepare(
    slot: TouchSlot, now: datetime, timezone: str, model: LanguageModel | None
) -> tuple[str, dict[str, object]] | None:
    """Текст и нагрузка касания вместе с раскачкой. `None` — сказать нечего.

    **Раскачка не отдельный слот, а приписка к существующим**, и это не экономия
    на схеме: `touch_slot` — тип в базе, новое значение стоит миграции, а второе
    сообщение в то же утро стоит владельца. Приписка при этом решает и главное
    — молчание слота превращается в повод сказать: до блока A утро без дел
    и без петли записывало `nothing_to_say` и не приходило вовсе, а это ровно
    диагноз фазы («бот не даёт повода что-то сказать»).

    **Один вопрос в сообщении** держится здесь и проверяется по готовому тексту
    слота, а не перебором случаев: сводка с петлёй уже кончается «Сдвинулось?»,
    и вторая вопросительная строка под ней — ловушка допроса
    (`docs/prompting_sources.md`, §1.3).
    """
    if slot is TouchSlot.MORNING:
        # Утро собирается отдельно, и не ради удобства: с 6 сентября 2026
        # вопрос раскачки стоит в нём **в двух местах** — факт материалом
        # сверху, вопрос в конце, — а остальные слоты по-прежнему приписывают
        # его целиком снизу. Собрать это общим кодом значило бы сделать вид,
        # что сообщения устроены одинаково.
        return await _morning(now, timezone, model)

    prepared = await _content(slot, now, timezone, model)
    asked = await _prompting(slot, prepared, now, timezone, model)

    if asked is None:
        return prepared

    if prepared is None:
        # Слоту сказать нечего — тогда вопрос и есть всё сообщение. Кнопок
        # под ним нет, поэтому уйдёт оно беззвучно (`vision.md` §7.1).
        return prompting.alone(asked.whole), {
            "prompting": prompting.payload(asked),
            "prompting_only": True,
        }

    text, payload = prepared
    payload["prompting"] = prompting.payload(asked)
    return prompting.attach(text, asked.whole), payload


async def _morning(
    now: datetime, timezone: str, model: LanguageModel | None
) -> tuple[str, dict[str, object]] | None:
    """Утреннее сообщение целиком. `None` — сказать нечего.

    Порядок сборки диктуется тем, что каждый шаг знает не всё:

    1. содержание собирается первым — от него зависит, спрашивает ли сообщение
       уже о чём-то само (петля кончается «Сдвинулось?»);
    2. вопрос выбирается вторым, по **сухому** тексту: правило «один вопрос
       в сообщении» проверяется по готовым словам, а не перебором случаев;
    3. лид пишется третьим, и бюджет слов ему считается по тексту **вместе
       с материалом и вопросом** — иначе сорок секунд из `vision.md` §5.1
       обещаны про сообщение, а посчитаны про его половину.

    Сухой рендер зовётся здесь трижды и это ничего не стоит: он чистая функция
    от уже прочитанных данных.
    """
    # Таймзона нужна сборке с Фазы 5: «сегодня» для календаря — это сутки
    # владельца, а не сутки UTC.
    content = await morning.collect(now, timezone)

    if content.empty:
        asked = await _prompting(TouchSlot.MORNING, None, now, timezone, model)
        if asked is None:
            return None
        # Делам и петле сказать нечего — остаётся вопрос, и он несёт свой факт
        # с собой: «Что с тех пор изменилось?» само по себе не значит ничего.
        return prompting.alone(asked.whole), {
            "prompting": prompting.payload(asked),
            "prompting_only": True,
        }

    dry = render.morning(content)
    asked = await _prompting(TouchSlot.MORNING, (dry, {}), now, timezone, model)
    spent = len(render.morning(content, asked=asked).split())

    payload: dict[str, object] = {
        "commitment_ids": [item.id for item in content.commitments],
        "loop_id": content.loop.id if content.loop else None,
        # Правило про три откладывания — часть замысла, а не оформления
        # (`vision.md` §5.2), поэтому признак считается в ядре, а не в адаптере.
        # Считает его сам `Morning`: тот же признак выбирает вопрос в тексте
        # сводки, и посчитанный тут второй раз он мог бы разойтись с текстом
        # молча. В журнал попадает вместе с остальной нагрузкой: по строке
        # касания должно быть видно, какой вопрос ушёл.
        "loop_stuck": content.loop_stuck,
    }
    if asked is not None:
        payload["prompting"] = prompting.payload(asked)

    written = await _lead(model, content, now, timezone, spent=spent)
    return render.morning(content, written=written, asked=asked), payload


async def _prompting(
    slot: TouchSlot,
    prepared: tuple[str, dict[str, object]] | None,
    now: datetime,
    timezone: str,
    model: LanguageModel | None,
) -> prompting.Question | None:
    """Вопрос раскачки для этого слота. `None` — сегодня без него.

    Слоты — настройка, а не константа (решение владельца № 2), и по умолчанию
    там только утро: каждый слот это отдельный запрос в сутки в полном режиме,
    и упирается это в квоту, а не во вкус. `PROMPTING_SLOTS` читается **здесь,
    на каждом тике**, а не при импорте модуля: настройка, снятая один раз
    в момент загрузки, требовала бы перезапуска воркера — то есть была бы
    константой с лишним шагом.
    """
    if slot not in prompting.slots_from(get_settings()):
        return None
    if prepared is not None and not prompting.fits(prepared[0]):
        return None
    return await prompting.pick(now=now, timezone=timezone, model=model)


async def _content(
    slot: TouchSlot, now: datetime, timezone: str, model: LanguageModel | None
) -> tuple[str, dict[str, object]] | None:
    """Что слот говорит сам по себе. `None` — сказать нечего.

    **Модель зовётся здесь, то есть до того, как слот занят.** Порядок ровно
    тот же, что был: сначала собрать текст, потом занять место в журнале,
    потом отправить. Цена — при гонке двух тиков лид напишется дважды, а слот
    достанется одному; переставлять из-за этого нельзя, потому что занятый слот
    при упавшей сборке стоит целого дня молчания, а лишний вызов — одного
    запроса из двадцати. Гонка при этом умозрительная: воркер один и тикает
    раз в пять минут.
    """
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
    model: LanguageModel | None,
    content: morning.Morning,
    now: datetime,
    timezone: str,
    *,
    spent: int,
) -> guard.Written | None:
    """Лид сводки от модели. `None` — сводка уйдёт сухим рендером.

    Отбор уже сделан: в модель уходит ровно то, что владелец увидит, — те же
    три дела и та же петля. Возраст и состояние дела прикладываются, потому что
    без них лид получится пересказом количества; **счётчик откладываний петли
    не прикладывается**, и это решение владельца — см. `core/present/digest`.

    **Бюджет считает код.** Сухие строки уже написаны, и сколько слов осталось
    лиду, знает только тот, кто их поставил: сорок секунд из `vision.md` §5.1 —
    это про всё сообщение, а не про то, что написала модель. `spent` приходит
    готовым и считается **по тексту вместе с материалом и вопросом** — с
    6 сентября 2026 сообщение состоит не только из дел, и считать бюджет
    по одним делам значило бы обещать сорок секунд, а мерить половину.
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
        words=lead.WORD_BUDGET - spent,
    )


async def _send(
    slot: TouchSlot,
    chat_id: int,
    text: str,
    payload: dict[str, object],
    notifier: Notifier,
) -> int:
    """Отправить — тем способом, который подходит слоту. Вернуть номер сообщения.

    Номер нужен раскачке: ответ владельца узнаётся по тому, на какое сообщение
    он отвечает, и другого способа связать сказанное с вопросом нет.
    """
    if payload.get("prompting_only") is True:
        # Слоту нечего было сказать, и всё сообщение — это вопрос. Кнопок
        # под ним нет, отвечают на него словами, значит уходит он так же,
        # как прочие инициативные касания: беззвучно (`vision.md` §7.1).
        return await notifier.send_quietly(chat_id, text)

    if slot is TouchSlot.MORNING:
        loop_id = payload.get("loop_id")
        return await notifier.send_morning(
            chat_id,
            text,
            commitment_ids=[int(item) for item in _ids(payload, "commitment_ids")],
            loop_id=int(loop_id) if isinstance(loop_id, int) else None,
            loop_stuck=payload.get("loop_stuck") is True,
        )

    if slot is TouchSlot.EVENING:
        return await notifier.send_evening(
            chat_id, text, entry_ids=[int(item) for item in _ids(payload, "entry_ids")]
        )

    # Всплывание и ревью — беззвучно и без кнопок (`vision.md` §7).
    return await notifier.send_quietly(chat_id, text)


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
