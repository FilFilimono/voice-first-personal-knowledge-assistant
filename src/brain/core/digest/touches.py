"""Журнал касаний и бюджет.

Главное про этот модуль: **молчание записывается так же, как отправка.** Без
этого правило «нечего дать — молчим» (`vision.md` §7.0, принцип 3) превращает
систему в неотличимую от мёртвой, а именно так пряталась каждая из восьми дыр
Фаз 1–2.

Бюджет считается запросом к журналу, а не счётчиком в памяти: перезапуск воркера
бывает чаще, чем кончаются сутки, и счётчик обнулялся бы вместе с процессом.

Защита от двойной отправки живёт в базе (`UNIQUE (slot, local_date)`), а не
в проверке «а не отправляли ли уже». Проверка в коде здесь не работает
по той же причине, по которой она не работает у дедупликации записей: два
срабатывания успеют прочитать пусто.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain.storage.db import session_scope
from brain.storage.models.touch import SilenceReason, Touch, TouchCategory, TouchSlot

# Три инициативных касания в сутки (`vision.md` §7). При нынешних слотах
# бюджет почти не ограничивает — инициативных слотов ровно три, — но правило
# записано не под сегодняшний набор слотов, а под завтрашний.
INITIATIVE_BUDGET = 3

# Категория выводится из слота, а не приходит от вызывающего: иначе одно и то же
# касание однажды окажется записанным в две разные категории, и бюджет начнёт
# считать не то. Заказанной категории (напоминания) здесь нет — у них своя
# таблица и свой путь, журнал касаний их не касается.
CATEGORIES: dict[TouchSlot, TouchCategory] = {
    TouchSlot.MORNING: TouchCategory.MORNING,
    TouchSlot.DAY: TouchCategory.INITIATIVE,
    TouchSlot.EVENING: TouchCategory.INITIATIVE,
    TouchSlot.WEEKLY: TouchCategory.INITIATIVE,
}


@dataclass(frozen=True, slots=True)
class Budget:
    """Сколько инициативных касаний потрачено за сутки владельца."""

    spent: int
    limit: int = INITIATIVE_BUDGET

    @property
    def left(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.left == 0


async def record_sent(slot: TouchSlot, day: date, *, payload: dict[str, Any] | None = None) -> bool:
    """Записать отправленное касание. `False` — за эти сутки слот уже занят.

    Возвращаемое значение — не формальность: именно оно решает, отправлять ли
    сообщение. Порядок в вызывающем коде поэтому обратный ожидаемому — сначала
    занять слот в журнале, потом отправлять. Отправив первым, при гонке двух
    тиков мы получили бы два одинаковых сообщения и один журнал.
    """
    return await _record(slot, day, sent_at=datetime.now(UTC), reason=None, payload=payload)


async def record_silence(slot: TouchSlot, day: date, *, reason: SilenceReason) -> bool:
    """Записать, что касания не было и почему. `False` — слот уже занят."""
    return await _record(slot, day, sent_at=None, reason=reason, payload=None)


async def _record(
    slot: TouchSlot,
    day: date,
    *,
    sent_at: datetime | None,
    reason: SilenceReason | None,
    payload: dict[str, Any] | None,
) -> bool:
    async with session_scope() as session:
        written = await session.scalar(
            pg_insert(Touch)
            .values(
                slot=slot,
                category=CATEGORIES[slot],
                local_date=day,
                sent_at=sent_at,
                silence_reason=reason,
                payload=payload or {},
            )
            .on_conflict_do_nothing(index_elements=[Touch.slot, Touch.local_date])
            .returning(Touch.id)
        )
    return written is not None


async def remember_message(slot: TouchSlot, day: date, *, message_id: int) -> None:
    """Дописать в нагрузку номер ушедшего сообщения.

    Единственное место, где строка журнала правится после записи, и правится
    она **после отправки**: до неё номера не существует. Ради него порядок
    «сначала занять слот, потом отправлять» не меняется — занятый слот при
    упавшей отправке стоит одного дня, а перевёрнутый порядок стоил бы двух
    одинаковых сообщений при гонке тиков.

    Нужен он раскачке: ответ владельца узнаётся по тому, на какое сообщение
    он отвечает (`digest/prompting.answered`). Своей таблицы ожидающих вопросов
    в проекте нет — состояние вопроса живёт в самой переписке, как у «когда
    напомнить?».
    """
    async with session_scope() as session:
        row = await session.scalar(select(Touch).where(Touch.slot == slot, Touch.local_date == day))
        if row is None:  # pragma: no cover — строку только что вставили сами
            return
        # Присваивание новым словарём, а не правкой на месте: JSONB
        # не отслеживает изменения внутри, и правка вложенного ключа уехала бы
        # в никуда молча.
        payload = dict(row.payload)
        payload["prompting"] = {**dict(payload.get("prompting") or {}), "message_id": message_id}
        row.payload = payload


async def of_day(day: date) -> dict[TouchSlot, Touch]:
    """Что уже случилось с каждым слотом за эти сутки — отправка или молчание."""
    async with session_scope() as session:
        rows = (await session.scalars(select(Touch).where(Touch.local_date == day))).all()
    return {row.slot: row for row in rows}


async def budget(day: date) -> Budget:
    """Бюджет инициативных касаний на сутки.

    Молчание бюджета не тратит: промолчали — значит не потратили, иначе тишина
    съедала бы право сказать. Это не мелочь при архиве, на котором «нечего
    сказать» — основной режим (ADR-0013).
    """
    async with session_scope() as session:
        spent = await session.scalar(
            select(func.count())
            .select_from(Touch)
            .where(
                Touch.local_date == day,
                Touch.category == TouchCategory.INITIATIVE,
                Touch.sent_at.is_not(None),
            )
        )
    return Budget(spent=int(spent or 0))


async def last(slot: TouchSlot) -> Touch | None:
    """Последнее, что было с этим слотом. Нужно `/status`."""
    async with session_scope() as session:
        found: Touch | None = await session.scalar(
            select(Touch).where(Touch.slot == slot).order_by(Touch.local_date.desc()).limit(1)
        )
    return found


async def shown_in_evening() -> set[int]:
    """Какие записи вечерний разбор уже показывал. Все, без срока давности.

    Найдено повтором по живому архиву 19 августа 2026: без этой проверки одна
    и та же пачка из трёх спорных записей уходила бы **каждый вечер** — семь
    вечеров подряд, пока долг не зафиксируется сам. Повторение одного и того же
    сообщения и есть та усталость от уведомлений, из-за которой систему бросают
    (`vision.md` §7.0).

    Срока давности здесь нет намеренно, в отличие от всплывания старого: там
    повтор через 90 дней — фича, здесь повтор не нужен вовсе. Не ответили —
    запись зафиксируется сама через семь дней и молча.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(Touch.payload["entry_ids"]).where(
                Touch.slot == TouchSlot.EVENING, Touch.sent_at.is_not(None)
            )
        )
    return {int(value) for row in rows if row for value in row}


async def deeds_shown() -> dict[int, date]:
    """Когда каждое дело последний раз показывалось в утренней сводке.

    Основание ротации, заведённой 6 сентября 2026. Претензия владельца
    дословно: «висит раз, два, три и ещё 64. Эти три я вижу каждое утро, они
    одинаковые». Так и было по построению: отбор шёл по возрасту, а старейшие
    из шестидесяти семи дел каждое утро одни и те же.

    Считается **по журналу касаний**, а не колонкой «показано» у дела:
    журнал уже хранит, что именно ушло, и вторая копия однажды разошлась бы
    с первой молча — та же причина, по которой здесь живут `shown_in_evening`
    и `resurfaced_since`. Заодно колонка с числом показов рано или поздно
    прочиталась бы вслух, а это счётчик (`vision.md` §7.0, п.5).
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Touch.local_date, Touch.payload["commitment_ids"]).where(
                    Touch.slot == TouchSlot.MORNING, Touch.sent_at.is_not(None)
                )
            )
        ).all()

    seen: dict[int, date] = {}
    for day, ids in rows:
        for value in ids or []:
            deed_id = int(value)
            if seen.get(deed_id, date.min) < day:
                seen[deed_id] = day
    return seen


async def resurfaced_since(day: date) -> set[int]:
    """Какие записи уже всплывали начиная с этих суток.

    Отдельной таблицы под правило «не чаще раза в 90 дней» (`vision.md` §5.3)
    нет намеренно: журнал уже хранит, что именно ушло, и вторая копия однажды
    разойдётся с первой — ровно как разошлась бы таблица перевода типов,
    от которой отказался ADR-0011.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(Touch.payload["entry_id"].astext).where(
                Touch.slot == TouchSlot.DAY,
                Touch.local_date >= day,
                Touch.sent_at.is_not(None),
            )
        )
    return {int(value) for value in rows if value is not None}
