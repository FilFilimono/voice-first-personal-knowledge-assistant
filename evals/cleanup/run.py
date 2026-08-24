"""Замер чистки. Режим: **по одной записи**, как её чистит воркер.

Пачками мерить нельзя: воркер чистит по одной, а на пачке модель видит соседние
записи и ведёт себя иначе. Ровно этой ошибкой два дня держался неверный
диагноз про темы (`status.md`, «Диагноз выше был неверен»).

Два уровня:
  1. код по всем голосовым архива — выдумки, числа, имена, отрицания, длина;
  2. набор владельца — сколько смысловых единиц дожило до чистой версии.

Ключ --model сравнивает модели на одном наборе.

    just eval-cleanup
    just eval-cleanup --model gemini-3.5-flash-lite
    just eval-cleanup --limit 10

**Латентность — не побочный результат, а вторая половина ответа.**
`CLEANUP_WAIT_SEC` сегодня равен тридцати секундам, взятым из головы. Если
настоящая чистка идёт дольше, владелец будет видеть дословный вариант
**всегда**, а подмена приходить потом — то есть показ вычищенного текста
выродится в отложенную правку сообщения. Поэтому время меряется по каждому
вызову отдельно и печатается медианой и p95.

**Ходит в тот же `present.clean_one`, что и воркер.** Копия промпта рядом
разошлась бы с рабочим на одну строку и перестала мерить прод. Отличается
ровно одно — метка в `llm_calls`: прогон по всему архиву это семьдесят
вызовов, и под меткой рабочей чистки они испортили бы ответ на вопрос
«сколько стоит чистка».

**Печатает живые тексты записей.** Это терминал владельца, и так и задумано:
замечания надо читать глазами. Перенаправлять вывод в файл внутри репозитория
нельзя — архив в git не уходит (ADR-0002). В `results/` ложатся только числа
и номера записей.
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from brain.config import Settings, get_runtime_settings
from brain.core import llm, present
from brain.core.memory.repository import is_private_now, latest_transcript
from brain.core.ports import LanguageModel
from brain.storage.db import dispose_engine, session_scope
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.llm_call import LlmCall
from evals.cleanup import checks

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"
RESULTS = HERE / "results"

# Формулировки смысловых единиц. Лежат рядом, но в git не уходят: это архив
# (ADR-0002), а запрещён он **в репозитории**, а не на диске владельца.
# Перечислен в `evals/cleanup/.gitignore` — так же, как кэш живого архива
# в `tests/data`.
UNITS = HERE / "units.jsonl"

# Своя метка в `llm_calls` — см. шапку. Прогон стоит денег и ест квоту,
# и то и другое обязано быть видно там же, где остальное.
PURPOSE = "eval-cleanup"

# Сколько нужно точек, чтобы называть перцентиль перцентилем. При меньшем
# числе ранг без интерполяции даёт ровно максимум: ceil(0.95 · n) = n при
# n ≤ 19. Настройка, выбранная по одной неудачной записи, — это не «p95».
MIN_POINTS_FOR_P95 = 20


@dataclass(frozen=True, slots=True)
class Target:
    """Запись, которую предстоит вычистить: номер и её дословный текст."""

    entry_id: int
    verbatim: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """Что вышло из одной записи.

    Два разных «не получилось», и путать их нельзя. `failure` — вызов упал,
    ответа не было вовсе. `clean is None` при пустом `failure` — модель
    ответила, но взять из ответа нечего: показ деградирует на дословный текст.
    Во втором случае владелец **ждал ровно столько же**, поэтому в латентность
    такой вызов входит, а в первом — нет.
    """

    entry_id: int
    verbatim: str
    clean: str | None
    report: checks.Report | None
    latency_ms: int
    failure: str | None = None

    @property
    def answered(self) -> bool:
        """Ответ пришёл — годный или пустой. Это и есть то, чего владелец ждал."""
        return self.failure is None


@dataclass(frozen=True, slots=True)
class Run:
    """Итог прогона вместе с тем, полным ли он был.

    `planned` и `interrupted` едут рядом с исходами намеренно: оборванный
    квотой прогон обязан быть отличим от полного и в заголовке, и в файле.
    Файлы результатов потом сравниваются между собой, и «сорок из семидесяти»,
    прочитанное как «семьдесят», — это сравнение с самим собой не по делу.
    """

    outcomes: list[Outcome]
    planned: int
    interrupted: str | None = None


@dataclass(frozen=True, slots=True)
class Marked:
    """Строка набора владельца: сколько смысловых единиц он насчитал в записи."""

    entry_id: int
    units: int
    note: str


@dataclass(frozen=True, slots=True)
class Survived:
    """Ответ владельца по одной записи: сколько её единиц дожило до чистой версии."""

    entry_id: int
    units: int
    survived: int


async def targets(limit: int | None) -> list[Target]:
    """Голосовые записи архива с дословной расшифровкой.

    Голосовые, а не все: чистка заведена ради расшифровки, у текстовой записи
    чистить нечего. Приватные отсечены здесь же — инвариант 5. Формально
    приватного голосового быть не может (голос в приватном режиме не
    принимается), но приватность включается **задним числом**, и дверь
    `core/llm` в этом случае подняла бы исключение на середине прогона.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Entry.id, latest_transcript())
                .where(Entry.kind == EntryKind.VOICE, ~is_private_now())
                .order_by(Entry.id)
            )
        ).all()

    found = [Target(entry_id=int(row[0]), verbatim=str(row[1])) for row in rows if row[1]]
    return found[:limit] if limit else found


def load_dataset(path: Path) -> list[Marked]:
    """Набор владельца. Пустой — законное состояние, второй уровень пропустится.

    Второй уровень не обязателен для первого: код по всему архиву меряет
    выдумки и латентность и без разметки. Но без набора замер отвечает только
    на половину вопроса — потерю смысла кодом не увидеть (см. `checks`).
    """
    if not path.exists():
        return []

    marked: list[Marked] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            marked.append(
                Marked(
                    entry_id=int(row["entry_id"]),
                    units=int(row["units"]),
                    note=row.get("note", ""),
                )
            )
        except (ValueError, KeyError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None
    return marked


async def database_now() -> datetime:
    """Время по часам базы — от него считается стоимость прогона.

    Не `datetime.now()`: строки в `llm_calls` штампует сервер, и разъехавшиеся
    на секунду часы отрезали бы первый вызов от суммы.
    """
    async with session_scope() as session:
        moment = await session.scalar(select(func.now()))
    return moment if isinstance(moment, datetime) else datetime.now(UTC)


def build_cleaner(settings: Settings, model_name: str | None) -> LanguageModel:
    """Тот же чистильщик, которого строит воркер, — `CLEANUP_MODEL`.

    **Здесь замер один раз уже врал бы молча.** `llm.build_model` при пустом
    имени подставляет `ENRICH_MODEL` — так и надо прогону разметки, и оттуда
    строка была переписана. Но воркер чистит `build_model(settings,
    model=settings.cleanup_model)` (`worker.py`), а модели разные: разметка
    идёт дешёвым `flash-lite`, чистка — `flash`. Прогон без ключа мерил бы
    разметочную модель и **подписывал её именем чистящей**, а `llm_calls`
    писал бы настоящее имя — то есть файл результата противоречил бы строке
    учёта. Оба решения, ради которых замер написан (какой моделью чистить
    и какой ставить `CLEANUP_WAIT_SEC`), снимались бы с неверного прогона.

    Поэтому имя модели в отчёте берётся **из построенной модели**, а не
    из настройки рядом: разойтись им тогда негде.
    """
    return llm.build_model(settings, model=model_name or settings.cleanup_model)


async def clean_all(items: list[Target], *, model: LanguageModel) -> Run:
    """Вычистить записи **по одной**, замеряя каждый вызов отдельно.

    Время берётся вокруг вызова, а не из ответа провайдера: владелец ждёт
    целиком то, что здесь измеряется, — вместе с учётом в `llm_calls`
    и разбором ответа. Именно с этим числом сравнивается `CLEANUP_WAIT_SEC`.
    """
    outcomes: list[Outcome] = []
    for item in items:
        started = time.perf_counter()
        try:
            clean = await present.clean_one(
                model,
                entry_id=item.entry_id,
                text=item.verbatim,
                is_private=False,
                purpose=PURPOSE,
            )
        except llm.LlmError as error:
            elapsed = int((time.perf_counter() - started) * 1000)
            outcomes.append(
                Outcome(
                    entry_id=item.entry_id,
                    verbatim=item.verbatim,
                    clean=None,
                    report=None,
                    latency_ms=elapsed,
                    failure=f"{type(error).__name__}: {error}"[:200],
                )
            )
            if error.status == 429:
                # Квота кончилась. Продолжать значит потратить ещё полсотни
                # отказов и получить ту же строку — прогон останавливается
                # и печатает то, что успел собрать.
                print(f"⚠️  Квота кончилась на записи {item.entry_id} — дальше не иду.\n")
                return Run(
                    outcomes=outcomes,
                    planned=len(items),
                    interrupted=f"квота (429) на записи {item.entry_id}",
                )
            continue

        elapsed = int((time.perf_counter() - started) * 1000)
        outcomes.append(
            Outcome(
                entry_id=item.entry_id,
                verbatim=item.verbatim,
                clean=clean,
                report=checks.compare(item.verbatim, clean) if clean is not None else None,
                latency_ms=elapsed,
            )
        )
    return Run(outcomes=outcomes, planned=len(items))


async def spent(since: datetime) -> tuple[int, Decimal | None]:
    """Сколько запросов и денег стоил прогон — по `llm_calls`, а не по счёту в уме.

    Квота у провайдера считается **в запросах** (ADR-0014), поэтому число
    запросов печатается наравне с деньгами: семьдесят вызовов за прогон — это
    заметная часть суточного лимита, и знать это надо до, а не после.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(func.count(), func.sum(LlmCall.cost_usd)).where(
                    LlmCall.purpose == PURPOSE, LlmCall.created_at >= since
                )
            )
        ).one()
    return int(row[0]), row[1]


def percentile(values: list[int], share: float) -> int:
    """Перцентиль по ближайшему рангу. Интерполяции нет намеренно.

    Записей десятки, а не тысячи: интерполированное значение выглядело бы
    точнее, чем есть, а решение принимается по порядку величины —
    «две секунды или сорок».
    """
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1))
    return ordered[index]


def report(
    outcomes: list[Outcome],
    *,
    model: LanguageModel,
    wait_sec: int,
    planned: int,
    interrupted: str | None,
) -> dict[str, object]:
    """Напечатать результат первого уровня и вернуть числа — для файла.

    Имя модели берётся из самой модели, а не приходит строкой рядом:
    настройка и построенная модель разъехались бы молча, см. `build_cleaner`.
    """
    # Пары «исход и его разбор» вместо `Outcome` с необязательным полем: иначе
    # каждая строка ниже переспрашивала бы, есть ли разбор, и читалось бы это
    # как «а вдруг нет», хотя ответ известен на первой строке.
    judged = [(outcome, outcome.report) for outcome in outcomes if outcome.report is not None]
    clean_ones = [outcome for outcome, report in judged if report.ok]
    share = len(clean_ones) / len(judged) if judged else 0.0

    counted = f"{len(outcomes)}" if len(outcomes) == planned else f"{len(outcomes)} из {planned}"
    print(f"Записей: {counted}   модель: {model.model}   режим: по одной записи")
    if interrupted:
        print(f"⚠️  Прогон оборван: {interrupted}. Числа ниже — по тому, что успело пройти.")
    print(f"Без замечаний: {share:.2f} ({len(clean_ones)} из {len(judged)})")

    # Пустой ответ модели из счёта **не выпадает**: владелец ждал его ровно
    # столько же, а деградация на дословный текст случается уже после ответа.
    # Выпадают только упавшие вызовы — там ждать было нечего.
    latencies = [outcome.latency_ms for outcome in outcomes if outcome.answered]
    median = int(statistics.median(latencies)) if latencies else 0
    p95 = percentile(latencies, 0.95)
    slowest = max(latencies, default=0)
    overdue = [item for item in latencies if item > wait_sec * 1000]
    # Ранг без интерполяции при n ≤ 19 даёт ровно максимум. Печатать такое
    # число под именем p95 и советовать по нему настройку — это выдать одну
    # неудачную запись за верхнюю границу нормы. README предлагает `--limit`
    # ради экономии квоты, так что случай не гипотетический.
    trustworthy = len(latencies) >= MIN_POINTS_FOR_P95

    print("\nЛатентность одного вызова — столько ждёт владелец:")
    print(f"  медиана   {median / 1000:.1f} с")
    print(
        f"  p95       {p95 / 1000:.1f} с" + ("" if trustworthy else "   ← это максимум, см. ниже")
    )
    print(f"  максимум  {slowest / 1000:.1f} с")
    print(f"  дольше CLEANUP_WAIT_SEC ({wait_sec} с): {len(overdue)} из {len(latencies)}")
    if trustworthy:
        print(
            f"  → CLEANUP_WAIT_SEC: не меньше {math.ceil(p95 / 1000)} с. Поставить меньше —\n"
            "    значит в каждом двадцатом случае показать дословный вариант, а вычищенный\n"
            "    дослать правкой сообщения."
        )
    else:
        print(
            f"  → Точек мало ({len(latencies)}, нужно {MIN_POINTS_FOR_P95}): p95 здесь совпадает\n"
            "    с максимумом и настройку по нему не выбирают. Прогнать без --limit."
        )

    with_findings = [(outcome, report) for outcome, report in judged if not report.ok]
    if with_findings:
        print(f"\nЗамечания ({len(with_findings)}) — читать глазами:")
        for outcome, found in with_findings:
            print(f"  запись {outcome.entry_id}: {'; '.join(found.findings)}")
            print(f"     было:  {outcome.verbatim}")
            print(f"     стало: {outcome.clean}")
    else:
        print("\nЗамечаний нет.")

    short = [(outcome, report) for outcome, report in judged if report.too_short]
    if short:
        print(
            f"\nКороче половины ({len(short)}) — **не нарушение**: чистка законно"
            f" сокращает.\nПосмотреть глазами, не потерялся ли кусок:"
        )
        for outcome, found in short:
            print(f"  запись {outcome.entry_id}: осталось {found.length_ratio:.2f}")
            print(f"     было:  {outcome.verbatim}")
            print(f"     стало: {outcome.clean}")

    failures = [outcome for outcome in outcomes if outcome.failure is not None]
    if failures:
        print(f"\nНе вычищено ({len(failures)}):")
        for failed in failures:
            print(f"  запись {failed.entry_id}: {failed.failure}")

    return {
        "режим": "по одной записи",
        "модель": model.model,
        "записей": len(outcomes),
        "задумано": planned,
        "оборван": interrupted,
        "вычищено": len(judged),
        "без замечаний": round(share, 4),
        "замечания": {
            "новые слова": sum(1 for _, report in judged if report.new_words),
            "числа": sum(1 for _, report in judged if report.lost_numbers),
            "имена": sum(1 for _, report in judged if report.lost_names),
            "отрицания": sum(1 for _, report in judged if report.lost_negations),
        },
        "записи с замечаниями": [outcome.entry_id for outcome, _ in with_findings],
        "короче половины": [outcome.entry_id for outcome, _ in short],
        "не вычищено": [outcome.entry_id for outcome in failures],
        "латентность мс": {"медиана": median, "p95": p95, "максимум": slowest},
        # Без этого признака два файла результатов сравнивались бы как равные,
        # хотя в одном p95 — это p95, а в другом — единственная худшая запись.
        "p95 достоверен": trustworthy,
        "cleanup_wait_sec": wait_sec,
        "дольше cleanup_wait_sec": len(overdue),
    }


def load_units(path: Path) -> dict[int, list[str]]:
    """Формулировки смысловых единиц — из файла, которого нет в git.

    **Без них второй прогон несравним с первым.** Главный сценарий замера —
    сравнение моделей ключом `--model`, то есть минимум два прогона. Если
    формулировок нет нигде, на втором прогоне владелец отвечает, восстанавливая
    собственную разметку по памяти, и разница между прогонами показывает
    не модель, а его счёт.

    Держать их в `dataset.jsonl` нельзя: это архив, а он в git не уходит
    (ADR-0002). Но ADR запрещает архив **в git**, а не архив на диске
    владельца, — поэтому файл лежит рядом и перечислен в `.gitignore`,
    ровно как кэш живого архива в `tests/data`.
    """
    if not path.exists():
        return {}

    units: dict[int, list[str]] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
            units[int(row["entry_id"])] = [str(unit) for unit in row["units"]]
        except (ValueError, KeyError, TypeError) as error:
            raise SystemExit(f"{path}:{number}: строка не разобрана — {error}") from None
    return units


def ask_number(question: str, *, default: int, top: int) -> int | None:
    """Спросить число, переспрашивая при опечатке. `None` — владелец прервал.

    Опечатка не имеет права стоить прогона: к этой минуте квота уже потрачена
    вся, а `review()` идёт последним. Ctrl-C и конец ввода не ошибка, а «хватит»,
    и обходятся так же — молча, с тем, что уже собрано.
    """
    while True:
        try:
            answer = input(question).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  прервано — записываю то, что уже отвечено")
            return None

        if not answer:
            return default
        if answer.isdigit() and int(answer) <= top:
            return int(answer)
        print(f"  нужно число от 0 до {top} или пустая строка")


def review(
    outcomes: list[Outcome], marked: list[Marked], units: dict[int, list[str]]
) -> dict[str, object]:
    """Второй уровень: сколько смысловых единиц дожило до чистой версии.

    **Считает не код, а владелец, и иначе быть не может.** Единица «решил
    не ехать, потому что всё равно отменят» не содержит ни цифр, ни имён —
    её пропажу не видит ни одна регулярка. Спросить об этом модель значит
    мерить согласие модели с собой; это правило действует на все наборы
    проекта.

    Поэтому прогон делает единственное, что может сделать честно: печатает
    **сами формулировки** единиц, кладёт рядом дословный и вычищенный текст
    и спрашивает по каждой записи отдельно. По записи, а не одной суммой:
    сравнение двух моделей иначе показало бы «14 против 12» и не показало бы,
    где именно.
    """
    by_id = {outcome.entry_id: outcome for outcome in outcomes}
    missing = [item.entry_id for item in marked if item.entry_id not in by_id]
    if missing:
        print(f"\n⚠️  Нет в прогоне: {missing} — записи нет в архиве либо её отрезал --limit")

    pairs = [(item, by_id[item.entry_id]) for item in marked if item.entry_id in by_id]
    expected = sum(item.units for item, _ in pairs)
    print(f"\nНабор владельца: записей {len(pairs)}, смысловых единиц {expected}")

    if not pairs:
        return {"записей": 0, "единиц": 0, "по записям": []}

    if not units:
        print(
            f"⚠️  Нет формулировок единиц ({UNITS.name}) — отвечать придётся по памяти,\n"
            "    и следующий прогон будет с этим несравним. См. evals/cleanup/README.md."
        )

    if not sys.stdin.isatty():
        for item, outcome in pairs:
            _show(item, outcome, units)
        print(
            "\nВвода нет (вывод перенаправлен) — сколько единиц дожило, не собрано.\n"
            "Прогнать в терминале: just eval-cleanup"
        )
        return {"записей": len(pairs), "единиц": expected, "по записям": []}

    answers: list[Survived] = []
    for item, outcome in pairs:
        _show(item, outcome, units)
        answer = ask_number(
            f"  сколько из {item.units} единиц дожило? [{item.units}] ",
            default=item.units,
            top=item.units,
        )
        if answer is None:
            break
        answers.append(Survived(entry_id=item.entry_id, units=item.units, survived=answer))

    survived = sum(row.survived for row in answers)
    counted = sum(row.units for row in answers)
    print(f"\nДожило единиц: {survived} из {counted} отвеченных (в наборе {expected}).")
    if survived < counted:
        print("Потерянных единиц должно быть ноль (ADR-0015, пункт 7) — чистка теряет смысл:")
        for row in answers:
            if row.survived < row.units:
                print(f"  запись {row.entry_id}: дожило {row.survived} из {row.units}")

    return {
        "записей": len(pairs),
        "единиц": expected,
        "отвечено записей": len(answers),
        "дожило": survived,
        "потеряно": counted - survived,
        # По записям, а не одной суммой: сравнивая два прогона, надо видеть,
        # **где** разошлось, иначе разница не указывает ни на что.
        "по записям": [
            {"запись": row.entry_id, "единиц": row.units, "дожило": row.survived} for row in answers
        ],
    }


def _show(item: Marked, outcome: Outcome, units: dict[int, list[str]]) -> None:
    """Показать запись целиком: разметку владельца и оба текста."""
    print(f"\n--- запись {item.entry_id} ({item.units} ед.) {item.note}")

    written = units.get(item.entry_id, [])
    for number, unit in enumerate(written, start=1):
        print(f"  {number}. {unit}")
    if written and len(written) != item.units:
        # Расхождение значит, что разметку меняли между прогонами, — и два
        # прогона перестали быть сравнимыми. Молча взять любое из двух чисел
        # хуже: сравнение продолжит выглядеть работающим.
        print(
            f"  ⚠️  в наборе {item.units} единиц, а выписано {len(written)} — разметка разъехалась"
        )
    print(f"  было:  {outcome.verbatim}")
    print(f"  стало: {outcome.clean}")


def save(summary: dict[str, object], *, stamp: datetime) -> Path:
    """Сохранить прогон. Только числа и номера записей — текстов здесь нет.

    Это не аккуратность, а ADR-0002: `results/` лежит в репозитории, а значит,
    попавшая сюда фраза владельца уедет в git навсегда. Выдуманные слова
    и потерянные имена печатаются в терминал и остаются в нём.
    """
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{stamp:%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Замер чистки расшифровки. Режим: по одной записи, как чистит воркер.",
        epilog=(
            "Пачками мерить нельзя: на пачке модель видит соседние записи "
            "и ведёт себя иначе. Второй уровень — набор владельца, evals/cleanup/README.md."
        ),
    )
    parser.add_argument("--model", default=None, help="имя модели вместо CLEANUP_MODEL")
    parser.add_argument("--limit", type=int, default=None, help="сколько записей взять")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--units", type=Path, default=UNITS)
    arguments = parser.parse_args()

    settings = get_runtime_settings()
    marked = load_dataset(arguments.dataset)
    units = load_units(arguments.units)
    stamp = datetime.now(UTC)

    try:
        items = await targets(arguments.limit)
        if not items:
            raise SystemExit(
                "В архиве нет голосовых записей с расшифровкой — мерить нечего.\n"
                "База поднята? just up && just migrate"
            )

        model = build_cleaner(settings, arguments.model)
        since = await database_now()
        finished = await clean_all(items, model=model)
        summary = report(
            finished.outcomes,
            model=model,
            wait_sec=settings.cleanup_wait_sec,
            planned=finished.planned,
            interrupted=finished.interrupted,
        )

        requests, cost = await spent(since)
        print(f"\nСтоил прогон: {requests} запросов, ${'?' if cost is None else f'{cost:.4f}'}")
        summary["запросов"] = requests
        summary["стоимость usd"] = None if cost is None else float(round(cost, 6))

        # Первый уровень записывается **до** второго и с ним не связан.
        # Второй идёт последним и ждёт человека: опечатка, Ctrl-C или закрытое
        # окно на двенадцатой записи из пятнадцати обнулили бы прогон, который
        # уже стоил всей квоты. Файл после ответов переписывается по тому же
        # пути — прогон один, файл один.
        print(f"\nЗаписано (первый уровень): {save(summary, stamp=stamp)}")

        if marked:
            summary["набор владельца"] = review(finished.outcomes, marked, units)
            print(f"\nЗаписано: {save(summary, stamp=stamp)}")
        else:
            print(
                "\nНабор владельца пуст — второй уровень пропущен.\n"
                "Код видит выдумки и пропажу чисел, имён и отрицаний; выкинутое"
                " придаточное\nне видит никто, кроме владельца. Как размечать —"
                " evals/cleanup/README.md."
            )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
