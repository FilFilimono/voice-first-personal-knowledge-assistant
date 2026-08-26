"""Чтение, которое нужно слою представления.

Отдельным файлом — как в `core/enrich`, `core/retrieval` и `core/memory`:
запросы живут в репозитории, а сервис решает, что из ответа следует. Смешать
их значит однажды искать `select` внутри функции, которая называется
«можно ли тратить запрос».
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update

from brain.core.llm import CHEAP
from brain.core.memory.repository import display_text, is_private_now
from brain.storage.db import session_scope
from brain.storage.models.commitment import LIVE_STATES, Commitment
from brain.storage.models.entry import Entry
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.loop import Loop, LoopStatus

# Что именно называется. Две таблицы, свои идентификаторы у каждой: дело № 5
# и петля № 5 — разные вещи, и в одной пачке номер 5 значил бы и то, и другое.
# Отсюда правило пачки в `present/titles`: дела и петли называются порознь.
COMMITMENT = "commitment"
LOOP = "loop"


async def refused_since(since: datetime, *, status_code: int) -> bool:
    """Отвечал ли провайдер текстовой модели этим кодом после `since`.

    Тир `cheap`, а не любой: у эмбеддингов своя квота и своя дверь, и их отказ
    к текстовой модели отношения не имеет. Смешать их значило бы замолчать
    чисткой из-за кончившейся индексации.
    """
    async with session_scope() as session:
        found = await session.scalar(
            select(LlmCall.id)
            .where(
                LlmCall.tier == CHEAP,
                LlmCall.status_code == status_code,
                LlmCall.created_at >= since,
            )
            .limit(1)
        )
    return found is not None


@dataclass(frozen=True, slots=True)
class TitleTarget:
    """То, чему нужно название, вместе с тем, можно ли показывать это наружу.

    Приватность едет рядом с текстом по тому же правилу, что в `ports.EntryText`
    и в `memory.CleanupTarget`: тот, кто собирается отправить текст в модель,
    обязан узнать про неё из того же запроса. Приватное отсеивается **здесь**,
    запросом к базе, — так же и по той же причине, что в разметке: дверь
    `core/llm` на приватную запись поднимает исключение, и одно просочившееся
    дело утащило бы за собой всю пачку.

    `text` — снимок смысловой единицы, а не кусок текста записи, вырезанный
    сейчас. Резать по отрезку пришлось бы **рабочий** текст (над ним считаны
    границы), а вычищенный по тем же границам дал бы чужой кусок — мина,
    описанная в `memory/repository.working_text`. Снимок от неё свободен
    и станет вычищенным сам, когда переключат `PRESENT_WORKING_TEXT`.
    """

    kind: str
    id: int
    text: str
    is_private: bool


async def targets_to_name(*, recompute: bool, limit: int | None) -> list[TitleTarget]:
    """Открытые дела и петли, которым нужно название. Дела первыми.

    **Порядок — от новых к старым.** Разметка зовёт это же чтение после
    каждой записи, и назвать она обязана то, что владелец только что сказал,
    а не хвост архива: старое разгребается командой `just backfill-titles`,
    в тот момент, когда владелец на это решился и видит цену в запросах.

    `recompute=False` берёт только безымянные — обычный случай. `True` берёт
    все открытые: без него смена модели или улучшение чистки оставили бы старые
    названия навсегда.

    Закрытые дела не берутся ни при каком ключе: их не показывает ни сводка,
    ни ревью, и запрос под квотой за их название платить не за что.
    """
    async with session_scope() as session:
        deeds = (
            await session.execute(
                select(Commitment.id, Commitment.text)
                .join(Entry, Entry.id == Commitment.source_entry_id)
                .where(
                    # Все живые состояния, а не только `open`: взятое в работу
                    # и отложенное владелец увидит в сводке, и безымянным оно
                    # там будет выглядеть так же плохо.
                    Commitment.state.in_(LIVE_STATES),
                    ~is_private_now(),
                    *([] if recompute else [Commitment.title.is_(None)]),
                )
                .order_by(Commitment.id.desc())
            )
        ).all()
        loops = (
            await session.execute(
                select(Loop.id, Loop.question)
                .join(Entry, Entry.id == Loop.source_entry_id)
                .where(
                    Loop.status == LoopStatus.OPEN,
                    ~is_private_now(),
                    *([] if recompute else [Loop.title.is_(None)]),
                )
                .order_by(Loop.id.desc())
            )
        ).all()

    targets = [
        TitleTarget(kind=COMMITMENT, id=int(row[0]), text=str(row[1]), is_private=False)
        for row in deeds
    ] + [
        TitleTarget(kind=LOOP, id=int(row[0]), text=str(row[1]), is_private=False) for row in loops
    ]
    return targets if limit is None else targets[:limit]


async def public_among(kind: str, ids: Sequence[int]) -> set[int]:
    """Какие из этих дел (или петель) заведены **не приватной** записью.

    Спрашивается перед тем, как собрать вход модели для сводки. Приватное дело
    из сводки при этом не исчезает — владельцу его собственная запись
    показывается, — но наружу не уходит (инвариант 5).

    Вопрос задаётся отдельным запросом, а не колонкой у уже прочитанного дела:
    приватность включается задним числом (`entry_overrides`), и «приватна ли
    запись» отвечает только `memory.is_private_now()`. Дело, прочитанное
    сводкой минуту назад, могло стать приватным нажатием кнопки.
    """
    if not ids:
        return set()

    table = Commitment if kind == COMMITMENT else Loop
    async with session_scope() as session:
        rows = await session.scalars(
            select(table.id)
            .join(Entry, Entry.id == table.source_entry_id)
            .where(table.id.in_(ids), ~is_private_now())
        )
    return {int(found) for found in rows.all()}


@dataclass(frozen=True, slots=True)
class Recorded:
    """Запись владельца в том виде, в каком он её сам видел."""

    entry_id: int
    text: str


async def recorded_between(start: datetime, end: datetime, *, limit: int) -> list[Recorded]:
    """Записи за отрезок времени — контекст, из которого пишется лид сводки.

    **Текст показной, а не рабочий.** Модель, пишущая для владельца, обязана
    читать то же, что читает он: дословная расшифровка с «э» и оборванными
    началами — это шум, за понимание которого платит вызов.

    Приватные не берутся вовсе, как и везде, где текст собирается наружу.
    Отбор по `created_at`, а не по дате разметки: «что записано вчера» —
    вопрос про день владельца, и разметка к нему отношения не имеет.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, display_text())
                .where(
                    Entry.created_at >= start,
                    Entry.created_at < end,
                    ~is_private_now(),
                )
                .order_by(Entry.id)
                .limit(limit)
            )
        ).all()

    return [Recorded(entry_id=int(row[0]), text=str(row[1])) for row in rows if row[1]]


async def save_titles(kind: str, named: dict[int, str]) -> int:
    """Записать названия. Вернуть, сколько строк изменилось.

    Перезапись, а не вставка «если пусто»: `--recompute` существует ровно
    затем, чтобы старое название уступило новому.
    """
    if not named:
        return 0

    table = Commitment if kind == COMMITMENT else Loop
    async with session_scope() as session:
        changed = 0
        for item_id, title in named.items():
            await session.execute(update(table).where(table.id == item_id).values(title=title))
            changed += 1
    return changed
