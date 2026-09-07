"""Эксплуатационные команды: то, что делается руками и изредка.

    uv run python -m brain.maintenance requeue-transcripts [--dry-run]

Отдельно от воркера намеренно: воркер крутит цикл и ничего не чинит,
а это одноразовые операции, которые запускает владелец, прочитав, что они делают.

**Когда что применять.** Упавшее задание, которое ещё лежит в очереди
со статусом failed, возвращается штатно:

    uv run pgq failed
    uv run pgq requeue <id> [<id> ...]

Команда отсюда нужна в другом случае — когда задания в очереди уже **нет**:
оно исчерпало попытки при `on_failure="delete"` (так было до 16 августа 2026),
либо процесс упал между записью и постановкой. Запись при этом цела —
инвариант 7 гарантирует, что не бывает наоборот, — но расшифровки у неё нет
и взяться ей неоткуда. Здесь список считается по самим данным: голосовая
запись без расшифровки, а не по журналу очереди.
"""

import argparse
import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

import brain.core.quota as quota
from brain.config import get_runtime_settings, get_settings
from brain.core import enrich as enrichment
from brain.core import llm, present
from brain.core.digest import commitments
from brain.core.enrich import repository as enrich_repository
from brain.core.llm import limits
from brain.core.memory.repository import working_text
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.ports import LanguageModel
from brain.core.present.repository import TitleTarget
from brain.core.retrieval import repository as retrieval_repository
from brain.core.scheduler.queue import ENRICH, INDEX, TRANSCRIBE
from brain.storage import schema
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.annotation import Annotation, AnnotationType
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.transcript import Transcript
from brain.storage.models.unit import EntryUnit

logger = logging.getLogger(__name__)


async def voice_entries_without_transcripts() -> list[Entry]:
    """Голосовые записи, у которых расшифровки нет.

    Это и есть определение «застрявшей» записи: очередь могла потерять задание,
    данные — нет.
    """
    async with session_scope() as session:
        result = await session.scalars(
            select(Entry)
            .where(
                Entry.kind == EntryKind.VOICE,
                ~select(Transcript.id).where(Transcript.source_entry_id == Entry.id).exists(),
            )
            .order_by(Entry.id)
        )
        return list(result)


async def requeue_transcripts(*, dry_run: bool) -> int:
    """Поставить заново задания на расшифровку. Вернуть число поставленных."""
    entries = await voice_entries_without_transcripts()

    if not entries:
        print("Записей без расшифровки нет.")
        return 0

    print(f"Голосовых без расшифровки: {len(entries)}")
    for entry in entries:
        print(f"  entry {entry.id}  {entry.created_at:%d.%m %H:%M}  {entry.audio_path}")

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    queued = 0
    for entry in entries:
        chat_id = entry.meta.get("chat_id")
        ack_message_id = entry.meta.get("ack_message_id")
        if not isinstance(chat_id, int) or not isinstance(ack_message_id, int):
            # Без них воркеру некуда положить результат. Пропускаем громко:
            # молчаливый пропуск здесь означал бы «починили», а это не так.
            print(f"  entry {entry.id}: пропущена, в meta нет chat_id/ack_message_id")
            continue

        async with capture_transaction() as uow:
            await uow.enqueue(
                TRANSCRIBE,
                {
                    "entry_id": entry.id,
                    "chat_id": chat_id,
                    "ack_message_id": ack_message_id,
                },
            )
        queued += 1

    print(f"\nПоставлено заданий: {queued}. Запустите воркер: just worker")
    return queued


async def reindex(*, dry_run: bool) -> int:
    """Поставить задания на индексацию всему, чему её не хватает.

    Список считается **по данным**, а не по журналу очереди — так же и по той
    же причине, что у `requeue-transcripts`. Три случая перечислены
    в `retrieval.repository.entries_needing_index`; общего у них одно:
    запись есть, а найти её нельзя.

    Ставится задание, а не считается вектор прямо здесь. Разница существенная:
    задание попадает под ретраи очереди и переживает падение провайдера,
    а цикл в этой команде на первой же сетевой ошибке остановился бы посередине
    архива — и понять, где именно, было бы нечем.
    """
    entry_ids = await retrieval_repository.entries_needing_index()

    if not entry_ids:
        print("Всё проиндексировано.")
        return 0

    print(f"Записей без индекса: {len(entry_ids)}")
    print("  " + ", ".join(str(entry_id) for entry_id in entry_ids))
    # Одна запись — один запрос к эмбеддингам: все её чанки уходят одним
    # батчем. Индексация архива поэтому упирается в квоту раньше разметки.
    await _report_cost(len(entry_ids), model=get_settings().embedding_model)

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for entry_id in entry_ids:
        async with capture_transaction() as uow:
            await uow.enqueue(INDEX, {"entry_id": entry_id})

    print(f"\nПоставлено заданий: {len(entry_ids)}. Запустите воркер: just worker")
    return len(entry_ids)


async def reenrich(*, dry_run: bool) -> int:
    """Поставить задания на разметку всему, что не размечено.

    Считается по данным — так же и по той же причине, что у двух команд выше.
    Приватные записи в список не попадают: разметки у них не бывает вовсе
    (инвариант 5), и это не «отставание», а нормальное состояние.

    Задание ставится одно на запись, но обработчик разберёт всё накопленное
    первой же пачкой (ADR-0011). Остальные задания найдут пустой список
    и честно ничего не сделают — так дешевле, чем выдумывать здесь пачки
    руками и разъезжаться с воркером в правилах отбора.
    """
    entries = await enrich_repository.entries_needing_annotation(limit=10_000)

    if not entries:
        print("Всё размечено.")
        return 0

    print(f"Записей без разметки: {len(entries)}")
    print("  " + ", ".join(str(entry.entry_id) for entry in entries))
    settings = get_settings()
    await _report_cost(
        enrichment.requests_for(len(entries), batch_limit=settings.enrich_batch_limit),
        model=settings.enrich_model,
    )

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for entry in entries:
        async with capture_transaction() as uow:
            await uow.enqueue(ENRICH, {"entry_id": entry.entry_id})

    print(f"\nПоставлено заданий: {len(entries)}. Запустите воркер: just worker")
    return len(entries)


async def backfill_commitments(*, dry_run: bool) -> int:
    """Завести дела по единицам, которые уже размечены. **Без вызова модели.**

    Нужна ровно один раз, при переходе на Фазу 4: дело заводится в транзакции
    разметки (ADR-0013), а разметка, сделанная до Фазы 4, про дела ничего
    не знала. Перепрогонять её ради этого незачем — единицы уже есть, дело
    выводится из них кодом, и модель здесь не нужна вовсе.

    Идемпотентна: повторный прогон дублей не заведёт, это держит уникальность
    по отрезку в базе.

    Записи, у которых **нет единиц вовсе** (разметка до ADR-0012), эта команда
    не чинит: вывести из них дела нечем. Их список печатается отдельно —
    им нужна переразметка, то есть `just reenrich --stale` и живая модель.
    """
    async with session_scope() as session:
        latest = (
            await session.execute(
                select(Annotation.source_entry_id, func.max(Annotation.id))
                .group_by(Annotation.source_entry_id)
                .order_by(Annotation.source_entry_id)
            )
        ).all()

    created = 0
    without_units: list[int] = []
    for entry_id, annotation_id in latest:
        async with session_scope() as session:
            has_units = await session.scalar(
                select(EntryUnit.id).where(EntryUnit.annotation_id == annotation_id).limit(1)
            )
            if has_units is None:
                without_units.append(int(entry_id))
                continue

            # Работа, а не показ: ниже из этой строки режутся дела по отрезкам
            # единиц, а отрезки посчитаны над рабочим текстом (ADR-0015, ось 3).
            body = await session.scalar(select(working_text()).where(Entry.id == entry_id))
            if dry_run:
                pending = await session.scalars(
                    select(EntryUnit.id).where(
                        EntryUnit.annotation_id == annotation_id,
                        EntryUnit.type == AnnotationType.TASK,
                    )
                )
                created += len(list(pending))
                continue

            created += await commitments.create_in(
                session,
                entry_id=int(entry_id),
                annotation_id=int(annotation_id),
                text=str(body or ""),
            )

    print(f"Дел {'нашлось бы' if dry_run else 'заведено'}: {created}")
    if without_units:
        print(
            f"\nБез смысловых единиц (разметка до ADR-0012): {len(without_units)} записей.\n"
            "  " + ", ".join(str(entry_id) for entry_id in without_units) + "\n"
            "  Им нужна переразметка: just reenrich --stale (нужна живая модель)."
        )
    if dry_run:
        print("\nЭто прогон вхолостую: ничего не записано. Уберите --dry-run.")
    return created


async def reenrich_stale(*, dry_run: bool) -> int:
    """Поставить переразметку записям, чья разметка сделана до ADR-0012.

    Такая разметка не имеет смысловых единиц, а из них выводятся дела Фазы 4
    и предложение завести петлю. Отдельно от `reenrich` намеренно: та ставит
    задания на **неразмеченное**, а это перепрогон уже размеченного.

    **Задания ставятся пачками с явным списком записей, а не по заданию
    на запись.** Первая редакция этой команды (19 августа 2026) ставила 37
    заданий без списка — и они бы ничего не сделали: обработчик отбирает
    неразмеченное, а у этих записей разметка есть. Нашлось разбором очереди
    назавтра, `attempts = 0` у всех тридцати семи.

    Заодно это честная цена: 37 записей — четыре запроса к модели, а не 37.

    Перепрогон добавляет строку разметки, а не правит старую (ADR-0011), так
    что откатывать нечего: не понравился результат — предыдущая разметка цела.
    """
    entry_ids = await enrich_repository.stale_entry_ids()

    if not entry_ids:
        print("Разметки без смысловых единиц нет.")
        return 0

    settings = get_settings()
    batch = settings.enrich_batch_limit
    batches = [entry_ids[start : start + batch] for start in range(0, len(entry_ids), batch)]
    planned = enrichment.requests_for(len(entry_ids), batch_limit=batch)

    print(f"Записей с разметкой до ADR-0012: {len(entry_ids)}")
    print("  " + ", ".join(str(entry_id) for entry_id in entry_ids))
    await _report_cost(planned, model=settings.enrich_model)

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не поставлено. Уберите --dry-run.")
        return 0

    for chunk in batches:
        async with capture_transaction() as uow:
            await uow.enqueue(ENRICH, {"entry_ids": chunk})

    print(f"\nПоставлено заданий: {len(batches)}. Запустите воркер: just worker")
    return len(batches)


# Сколько дел берёт `--preview` без числа. Десять — это ровно один вызов
# и десять названий подряд на экране: по меньшему судить не о чем, а большее
# уже не окинуть глазом, ради чего предпросмотр и затевался.
PREVIEW_DEFAULT = 10

# Отрезок печатается в одну строку: в расшифровке попадаются переводы строки,
# и без склейки блок «было/стало» развалился бы посреди сравнения.
_SPACES = re.compile(r"\s+")

# Как называется вид в предпросмотре. Из `present.titles` сюда приходят
# служебные `commitment`/`loop`, а читает вывод владелец.
_KINDS = {present.titles.COMMITMENT: "дело", present.titles.LOOP: "петля"}


async def backfill_titles(
    *,
    model: LanguageModel,
    dry_run: bool,
    recompute: bool,
    limit: int | None,
    preview: int | None = None,
) -> int:
    """Назвать дела и петли, которые заведены до Задачи 4 или названы старой моделью.

    Считается **по данным** (`title IS NULL` у открытых), а не по журналу
    очереди, — так же и по той же причине, что у соседних команд.

    **`--recompute` — не удобство, а условие, при котором задачу вообще можно
    было делать.** Команда, умеющая только заполнять пустое, закрепляет в базе
    результат первой попавшейся модели: смена `TITLES_MODEL` или улучшение
    чистки не тронули бы старые названия никогда. Ровно из-за этого Задачу 4
    и откладывали.

    **`--limit` нужен потому, что лимит бесплатного тира — 20 запросов в сутки
    на модель.** Владелец раскладывает прогон на части и видит цену до того,
    как её платит: `--dry-run` печатает, сколько запросов уйдёт и сколько
    сегодня уже потрачено **этой самой моделью** — суммой по тиру этот вопрос
    не решался, у соседних моделей лимиты другие.

    **`--preview N` отвечает на другой вопрос — не «сколько это стоит»,
    а «что получится».** Смета без единого названия предпросмотром не была:
    результат и есть то, ради чего в неё смотрят. Ключ берёт одну пачку,
    зовёт модель по-настоящему и не пишет в базу ничего.

    Модель приходит снаружи, а не строится здесь: её выбирает `main()` —
    и она же подставляется тестами, которым живой провайдер недоступен
    и не нужен.
    """
    # У предпросмотра своё N, и оно вытесняет `--limit`: оба говорят, сколько
    # дел взять, но `--limit` делит **запись** на дни, а предпросмотр ничего
    # не пишет и делить ему нечего. Оставить работать оба значило бы завести
    # два числа на один вопрос.
    targets = await present.titles.pending(
        recompute=recompute, limit=preview if preview is not None else limit
    )

    if not targets:
        print("Названия есть у всех открытых дел и петель.")
        return 0

    if preview is not None:
        return await _preview_titles(model, targets)

    deeds = [target for target in targets if target.kind == present.titles.COMMITMENT]
    loops = [target for target in targets if target.kind == present.titles.LOOP]
    print(f"Без названия: дел {len(deeds)}, петель {len(loops)}")
    if recompute:
        print("  --recompute: названия будут пересчитаны, включая уже проставленные.")

    # Пачки считаются по видам порознь: дело № 5 и петля № 5 — разные строки,
    # и в одном конверте их номера значили бы разное (см. `present/titles`).
    batch = present.titles.BATCH_LIMIT
    planned = present.titles.requests_for(len(deeds), batch_limit=batch)
    planned += present.titles.requests_for(len(loops), batch_limit=batch)
    # Модель спрашивается у самой модели, а не у настроек: прогон пойдёт той,
    # что пришла сюда параметром, — и если `main()` когда-нибудь подставит
    # другую, строка расхода поедет за ней, а не останется про `TITLES_MODEL`.
    await _report_cost(planned, model=model.model)

    if dry_run:
        print("\nЭто прогон вхолостую: ничего не записано. Уберите --dry-run.")
        return 0

    named = await present.titles.name(model, targets)
    print(f"\nНазваний проставлено: {named} из {len(targets)}.")
    if named < len(targets):
        print("  Остальные показываются прежним отрезком — модель или гвард их не приняли.")
    return named


async def _preview_titles(model: LanguageModel, targets: Sequence[TitleTarget]) -> int:
    """Показать одну пачку названий рядом с отрезками. **В базу не пишет.**

    Вернуть, сколько из показанного прошло гвард, — то же число, что легло бы
    в базу при настоящем прогоне.

    Печатается пара строк на дело: отрезок, который владелец видит сейчас,
    и название, которое встало бы на его место. Одного результата мало —
    по нему не видно, **что изменилось**, а решать владелец собирается именно
    это: стоит ли смена отрезка на название семидесяти строк в базе.
    """
    # Цена печатается до вызова, как у соседей: предупреждение после того, как
    # запрос ушёл, ничего не меняет. Вызов один на вид — пачка одна.
    planned = len({target.kind for target in targets})
    await _report_cost(planned, model=model.model)

    shown = await present.titles.preview(model, targets)
    if len(shown) < len(targets):
        print(
            f"\nВзято {len(shown)} из {len(targets)}: предпросмотр — это одна пачка,"
            f" а в неё помещается {present.titles.BATCH_LIMIT}."
        )

    print()
    for item in shown:
        print(f"{_KINDS.get(item.target.kind, item.target.kind)} {item.target.id}")
        print(f"было:  {_one_line(item.target.text)}")
        print(f"стало: {item.title or '—'}")
        if item.title is None:
            # Молчание модели ничем не отличается от отказа по последствиям:
            # дело останется с отрезком. Значит, и показано должно быть так же.
            print("       ✗ в базу не попадёт: модель не ответила про это дело")
        elif item.refusal is not None:
            print(f"       ✗ в базу не попадёт: {item.refusal}")
        print()

    passed = sum(1 for item in shown if item.title is not None and item.refusal is None)
    print(f"Прошло проверку: {passed} из {len(shown)}. В базу не записано ничего.")
    print("  Настоящий прогон: just backfill-titles")
    return passed


def _one_line(text: str) -> str:
    """Отрезок одной строкой и **целиком**.

    Потолок в сто символов стоял здесь до 27.08.2026 и стоил предпросмотру
    единственного, ради чего он есть. Название дела 79 собралось из слова
    на 146-м символе записи, а строка «было:» показывала первые сто: владелец
    не смог отличить слово записи от выдумки модели и прочитал верную работу
    гварда как дыру в нём. Длинную строку перенесёт терминал, и это дешевле,
    чем предпросмотр, которому нельзя верить.

    Переводы строки по-прежнему склеиваются — см. `_SPACES`.
    """
    return _SPACES.sub(" ", text).strip()


async def _report_cost(planned: int, *, model: str) -> None:
    """Сказать, во сколько запросов обойдётся команда и хватит ли остатка **этой модели**.

    Появилось после 20 августа 2026: суточная квота кончилась вслепую, потому
    что расход было неоткуда взять, а цену команды — неоткуда узнать.

    **Считается моделью, а не тиром** (с 26.08.2026). Тир складывает несравнимое:
    26 августа в тире `cheap` работали три модели, сумма по ним вышла 58, и число
    это не отвечало на единственный вопрос, ради которого строку читают, —
    пойдёт ли прогон сейчас. У `3.6-flash` в тот день двадцать запросов уже
    кончились, а `3.5-flash-lite` за те же сутки сделала двадцать два и не
    отказала ни разу. Лимит провайдер держит на модель и сказал это дословно:
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier` в теле `429`.

    Печатается **до** постановки заданий: предупреждение после того, как всё
    поставлено, ничего не меняет.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    overrides = limits.configured(settings)
    spending = {usage.model: usage for usage in await quota.by_model(now, overrides=overrides)}
    # Модели, которая сегодня ещё не звалась, в разбивке нет вовсе — это ноль
    # расхода, а не отсутствие ответа. Лимит у неё при этом известен заранее,
    # и промолчать о нём значило бы потерять единственную полезную половину строки.
    usage = spending.get(model) or quota.ModelUsage(
        model=model,
        spent=0,
        refused=0,
        limit=limits.daily_limit(model, override=overrides.get(model)),
    )

    print(f"\nЗапросов к модели уйдёт: {planned}")
    print(f"  {_spending(usage, planned)}")

    # Время сброса — в часах владельца, а не в UTC: строка кончается словами
    # «по вашему времени», и до 26.08.2026 они были неправдой на два часа.
    resets = quota.resets_at(now).astimezone(ZoneInfo(settings.brain_timezone))
    left = usage.left

    if left is not None and planned > left:
        print(
            f"  ⚠️  Не хватит: остаток {left}. Часть заданий упрётся в квоту\n"
            f"     и будет ждать сброса — он в {resets:%H:%M %d.%m} по вашему времени."
        )
    elif usage.refused:
        # Отказ провайдера вернее нашего счётчика: тем же ключом мог
        # пользоваться кто-то ещё, и тогда наш счёт занижен. Начинать прогон
        # после сегодняшнего `429` — это платить временем за тот же ответ.
        print(
            f"  ⚠️  Сегодня эта модель уже отказывала ({usage.refused}). Прогон может встать\n"
            f"     на первом запросе; сброс квоты в {resets:%H:%M %d.%m} по вашему времени."
        )


def _spending(usage: quota.ModelUsage, planned: int) -> str:
    """Строка про расход модели. Родня той, что `/status` шлёт в телефон.

    Формат намеренно тот же: одно и то же число, названное в двух местах
    по-разному, читается как два разных числа.
    """
    if usage.limit is None:
        # Лимит неизвестен — так и сказано. Выдуманное число здесь читалось бы
        # как измеренное, а у `3.5-flash-lite` границы никто не видел.
        line = f"{usage.model}: сегодня {usage.spent} — лимит неизвестен"
    else:
        left = usage.left or 0
        line = (
            f"{usage.model}: сегодня {usage.spent} из {usage.limit},"
            f" останется {max(0, left - planned)}"
        )

    if usage.refused:
        line += f", отказов {usage.refused}"

    return f"🔴 {line}" if usage.exhausted or usage.refused else line


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов отдельно от работы.

    Отдельной функцией — чтобы значения по умолчанию и взаимоисключения
    проверялись тестом. Они часть договора с владельцем: «`--preview` без числа
    стоит один вызов» — обещание, а не деталь, и жить оно должно там, где его
    можно прочитать проверкой.
    """
    parser = argparse.ArgumentParser(description="Эксплуатационные команды Brain")
    commands = parser.add_subparsers(dest="command", required=True)

    requeue = commands.add_parser(
        "requeue-transcripts",
        help="поставить заново расшифровку для голосовых, у которых её нет",
    )
    requeue.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )

    index = commands.add_parser(
        "reindex",
        help="поставить индексацию записям, которых нет в поиске",
    )
    index.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )

    annotate = commands.add_parser(
        "reenrich",
        help="поставить разметку записям, у которых её нет",
    )
    annotate.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )
    annotate.add_argument(
        "--stale",
        action="store_true",
        help="перепрогнать разметку, сделанную до ADR-0012 (без смысловых единиц)",
    )

    backfill = commands.add_parser(
        "backfill-commitments",
        help="завести дела по уже размеченным единицам, без вызова модели",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что было бы сделано, и ничего не менять",
    )

    titles_command = commands.add_parser(
        "backfill-titles",
        help="назвать открытые дела и петли, у которых названия нет",
    )
    # Смета и предпросмотр отвечают на разные вопросы и противоречат друг другу:
    # `--dry-run` обещает не звать модель, `--preview` зовёт её обязательно.
    # Argparse откажет вслух — молчаливое «один победил другого» оставило бы
    # владельца в уверенности, что цену он тоже увидел.
    without_writing = titles_command.add_mutually_exclusive_group()
    without_writing.add_argument(
        "--dry-run",
        action="store_true",
        help="показать цену прогона в запросах и ничего не менять",
    )
    without_writing.add_argument(
        "--preview",
        type=int,
        nargs="?",
        const=PREVIEW_DEFAULT,
        default=None,
        help=(
            "назвать одну пачку из N дел по-настоящему и показать названия "
            f"рядом с отрезками, не записывая ничего (по умолчанию {PREVIEW_DEFAULT}; "
            "N заменяет --limit)"
        ),
    )
    titles_command.add_argument(
        "--recompute",
        action="store_true",
        help="пересчитать и уже проставленные названия — после смены модели",
    )
    titles_command.add_argument(
        "--limit",
        type=int,
        default=None,
        help="сколько дел взять за прогон: суточный лимит модели — 20 запросов",
    )

    return parser


async def main() -> None:
    arguments = build_parser().parse_args()
    get_runtime_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        # Схема — до самой команды. С этого случая долг и начался: 26 августа
        # 2026 `just backfill-titles` упал `invalid input value for enum
        # commitment_state` — код знал новые состояния, база нет
        # (`storage/schema`). Внутри `try`, чтобы движок закрылся и на отказе:
        # проверка уже открыла соединение.
        await schema.require("Команда не выполнена.")

        if arguments.command == "requeue-transcripts":
            await requeue_transcripts(dry_run=arguments.dry_run)
        elif arguments.command == "reindex":
            await reindex(dry_run=arguments.dry_run)
        elif arguments.command == "reenrich":
            if arguments.stale:
                await reenrich_stale(dry_run=arguments.dry_run)
            else:
                await reenrich(dry_run=arguments.dry_run)
        elif arguments.command == "backfill-commitments":
            await backfill_commitments(dry_run=arguments.dry_run)
        elif arguments.command == "backfill-titles":
            # Модель строится здесь, а не внутри команды: команда получает её
            # готовой и потому проверяется без живого провайдера.
            settings = get_settings()
            await backfill_titles(
                model=llm.build_model(settings, model=settings.titles_model),
                dry_run=arguments.dry_run,
                recompute=arguments.recompute,
                limit=arguments.limit,
                preview=arguments.preview,
            )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
