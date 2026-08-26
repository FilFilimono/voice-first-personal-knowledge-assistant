"""Названия дел и петель: то, что владелец читает в сводке вместо обрезка.

Задача 4 волны ADR-0015. До неё дело показывалось первыми символами отрезка
смысловой единицы, обрезанными по счётчику: «Вот это по боту. В целом всё
остальное дней неизменно. Фильм, да, надо…». Теперь дело называется по-русски,
а обрезок остаётся запасным путём и не исчезает никогда.

**Что здесь доказывается и чем.** Тест на функцию тут не доказывает ничего:
он проверил бы, что строка короче пятидесяти символов и непустая, — а это ровно
то, что уже умеет обрезок. Поэтому утверждения идут от входов системы:
от задания разметки (`jobs.enrich`) и от тика касаний до текста, ушедшего
отправителю, и от команды обслуживания до строк в базе.

Модель заменена заглушкой, и это честно: все утверждения файла — про путь,
про пачку и про отказ, а не про качество названия. Качество меряется живым
прогоном, и его делает владелец: суточный лимит `gemini-3.6-flash` — двадцать
запросов.

Тексты — живые, по номерам записей (ADR-0002: архив в git не уходит).
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from brain import maintenance
from brain.config import Settings
from brain.core import present
from brain.core.digest import loops, service
from brain.core.memory import overrides
from brain.core.ports import Completion
from brain.core.present import titles
from brain.core.scheduler import jobs
from brain.storage.db import session_scope
from brain.storage.models.commitment import Commitment
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.loop import Loop
from tests import archive as archive_module
from tests.fakes import FakeNotifier, ScriptedModel, StubEmbedder, answer_units

TIMEZONE = "Europe/Rome"
CHAT_ID = 111

# 09:30 по Риму — внутри утреннего окна. Тот же момент, что в `test_digest_tick`.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)

# Живая запись, на которой гоняется весь путь. Выбрана за то, что в ней два
# дела подряд и настоящая граница между ними: «Фильм, да, надо будет
# посмотреть» начинается на 55-м символе — так эта запись и разрезана
# в рабочей базе.
LIVE_ENTRY = 53
SECOND_DEED = "Фильм, да,"

# Название, которое модель могла бы написать по второму делу этой записи.
FILM_TITLE = "Досмотреть фильм"


# --- Заглушка модели названий ---


class Titler:
    """Модель, которая называет то, что ей дали, и считает вызовы.

    Идентификаторы берутся **из промпта**, а не задаются тестом: до вызова их
    неоткуда знать — дела заводит разметка, и номера у них появляются в базе.
    Это же делает заглушку пригодной для главного утверждения файла: пачка
    уходит одним вызовом, и в одном промпте видны все дела сразу.
    """

    def __init__(self, *names: str, answer: dict[str, Any] | None = None) -> None:
        self._names = list(names)
        self._answer = answer
        self.prompts: list[str] = []

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return "stub-titles"

    @property
    def tier(self) -> str:
        return "cheap"

    @property
    def calls(self) -> int:
        return len(self.prompts)

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        temperature: float,
    ) -> Completion:
        self.prompts.append(prompt)
        if self._answer is not None:
            return Completion(data=self._answer, input_tokens=1, output_tokens=1, latency_ms=1)

        ids = [int(found) for found in re.findall(r"^\[(\d+)\]", prompt, flags=re.MULTILINE)]
        items = [
            {"id": item_id, "title": self._names[index % len(self._names)]}
            for index, item_id in enumerate(ids)
        ]
        return Completion(data={"items": items}, input_tokens=10, output_tokens=10, latency_ms=1)


# --- Проверка названия перед записью в базу. Без базы и без сети ---

SOURCE = "Фильм, да, надо будет посмотреть. Минимум 10 страниц."


def test_guard_lets_a_plain_title_through() -> None:
    """Название из слов записи проходит — иначе гвард просто выключил бы модель."""
    assert guard_reason("Досмотреть фильм") is None


def test_guard_refuses_an_invented_number() -> None:
    """Числа, которого не было во входе, в названии быть не может.

    Название лежит в базе и показывается месяцами: выдуманное «за 5 дней»
    читается как срок, который владелец себе ставил, — а он не ставил.
    """
    assert guard_reason("Досмотреть фильм за 5 дней") is not None


def test_guard_keeps_a_number_that_came_from_the_input() -> None:
    assert guard_reason("Прочитать 10 страниц") is None


def test_guard_refuses_an_invented_name() -> None:
    """Имя, которого во входе не было, — выдумка того же рода, что число."""
    assert guard_reason("Досмотреть фильм с Мишей") is not None


def test_guard_keeps_a_declined_name() -> None:
    """Имя из записи, поставленное в другой падеж, названием не выдумано.

    Русский склоняет имена, и требовать буквального совпадения значило бы
    отвергать каждое второе название с именем — то есть показывать обрезок там,
    где всё в порядке.
    """
    assert guard_reason("Написать Мише", source="Надо написать Мишей вечером") is None


def test_guard_refuses_a_latin_word_that_was_not_there() -> None:
    """Латиница ловится и с маленькой буквы: `find` из записи — имя команды."""
    assert guard_reason("Починить grep", source="Нужно ещё find починить") is not None
    assert guard_reason("Починить find", source="Нужно ещё find починить") is None


def test_guard_refuses_a_title_longer_than_the_cap() -> None:
    """Длинное название не подрезается, а отвергается.

    Подрезка по счётчику — ровно то, от чего уходит Задача 4: обрезок,
    написанный моделью, ничем не лучше обрезка, написанного кодом.
    """
    assert guard_reason("Досмотреть " * 20) is not None


def test_guard_refuses_an_empty_title() -> None:
    assert guard_reason("   ") is not None


def test_guard_refuses_a_reproach() -> None:
    """Название читается каждое утро, и упрёк в нём стоит дороже, чем в лиде."""
    assert guard_reason("Так и не досмотреть фильм") is not None


def guard_reason(title: str, source: str = SOURCE) -> str | None:
    return present.guard.check_title(title, source=source, max_length=titles.MAX_LENGTH)


# --- Разбор ответа модели ---


def test_parse_takes_the_title() -> None:
    parsed = titles.parse({"items": [{"id": 7, "title": FILM_TITLE}]}, sources={7: SOURCE})

    assert parsed == {7: FILM_TITLE}


def test_parse_refuses_a_foreign_id() -> None:
    """Чужой идентификатор — та же защита, что в разметке и в чистке.

    Здесь она дороже: название уедет в базу и будет показываться месяцами.
    """
    assert titles.parse({"items": [{"id": 8, "title": FILM_TITLE}]}, sources={7: SOURCE}) == {}


def test_parse_drops_a_title_the_guard_refused() -> None:
    """Отвергнутое название не пишется, а дело остаётся без него — с обрезком."""
    answer = {"items": [{"id": 7, "title": "Досмотреть фильм за 5 дней"}]}

    assert titles.parse(answer, sources={7: SOURCE}) == {}


def test_parse_survives_an_answer_without_items() -> None:
    """Ответ не по схеме — пусто, а не исключение: название не стоит разметки."""
    assert titles.parse({}, sources={7: SOURCE}) == {}


def test_requests_for_counts_batches_not_deeds() -> None:
    """Семьдесят два дела — три запроса, а не семьдесят два.

    Суточный лимит `gemini-3.6-flash` на бесплатном тире — двадцать запросов
    на модель, и вызов на дело съел бы его за один прогон.
    """
    assert titles.requests_for(72, batch_limit=titles.BATCH_LIMIT) == 3
    assert titles.requests_for(0, batch_limit=titles.BATCH_LIMIT) == 0


def test_titles_model_is_its_own_setting() -> None:
    """Модель названий не наследует модель чистки.

    Название лежит в базе и показывается месяцами, а вычищенный текст правится
    следующим прогоном: модель, переписывающая имена, в названиях опаснее.
    """
    assert Settings(cleanup_model="gemini-3.5-flash-lite").titles_model == "gemini-3.6-flash"


# --- Путь целиком: от разметки до строки в сводке ---


async def _live_entry(url: str, entries: list[archive_module.Entry], number: int) -> int:
    """Положить живую запись в одноразовую базу."""
    source = archive_module.entry(entries, number)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.TEXT,
                    raw_text=source.text,
                    is_private=source.is_private,
                    timezone=TIMEZONE,
                    dedup_key=f"live:{number}",
                    meta={"chat_id": CHAT_ID, "ack_message_id": 1},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _annotate(entry_id: int, text: str, titler: Titler) -> None:
    """Разметить запись так же, как это делает воркер, — вместе с названиями."""
    split = text.index(SECOND_DEED)
    model = ScriptedModel(
        [answer_units(entry_id, ("начало записи", "task"), (text[split : split + 20], "task"))]
    )
    await jobs.enrich(
        entry_id=entry_id,
        model=model,
        embedder=StubEmbedder(),
        notifier=FakeNotifier(),
        limit=25,
        titler=titler,
    )


async def _commitment_titles() -> list[str | None]:
    async with session_scope() as session:
        rows = await session.scalars(select(Commitment.title).order_by(Commitment.id))
    return list(rows.all())


@pytest.mark.integration
async def test_summary_shows_the_written_title_instead_of_the_span(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Главное утверждение задачи: в сводке стоит название, а не кусок речи."""
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    titler = Titler("Разобраться с ботом", FILM_TITLE)

    await _annotate(entry_id, text, titler)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, shown, _, _ = notifier.mornings[0]
    assert FILM_TITLE in shown
    assert "Вот это по боту" not in shown
    # Пачка, а не вызов на дело: два дела ушли одним запросом.
    assert titler.calls == 1


@pytest.mark.integration
async def test_summary_keeps_the_span_when_the_guard_refuses_the_title(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Отказ гварда прячет название, а не дело.

    Жёсткое требование задачи: дело, у которого название не вышло,
    показывается прежним обрезком и из сводки не исчезает никогда.
    """
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)

    await _annotate(entry_id, text, Titler("Досмотреть фильм за 5 дней"))

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, shown, commitment_ids, _ = notifier.mornings[0]
    assert len(commitment_ids) == 2
    assert "Вот это по боту" in shown
    assert "за 5 дней" not in shown


@pytest.mark.integration
async def test_summary_keeps_the_span_when_the_model_says_nothing(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Модель не ответила — сводка приходит прежней, а разметка не падает."""
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)

    await _annotate(entry_id, text, Titler(answer={}))

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, shown, commitment_ids, _ = notifier.mornings[0]
    assert len(commitment_ids) == 2
    assert "Вот это по боту" in shown
    assert await _commitment_titles() == [None, None]


@pytest.mark.integration
async def test_a_private_entry_never_goes_out_for_a_title(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Приватность включена задним числом — дело остаётся без названия (инвариант 5)."""
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _annotate(entry_id, text, Titler(answer={}))
    await overrides.make_private(entry_id)

    titler = Titler(FILM_TITLE)
    named = await titles.name_open(titler, limit=None, recompute=True)

    assert titler.calls == 0
    assert named == 0
    assert await _commitment_titles() == [None, None]


@pytest.mark.integration
async def test_the_loop_question_gets_a_title_too(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Петля называется так же, как дело: в сводке она — первая строка вопроса."""
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await loops.create(entry_id=entry_id, unit_id=None, question=text[:200])

    titler = Titler("Разобраться с подпиской")
    await titles.name_open(titler, limit=None, recompute=False)

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, shown, _, _ = notifier.mornings[0]
    assert "Ты хотел разобраться: Разобраться с подпиской" in shown


# --- Команда обслуживания ---


async def _deeds(url: str, entry_id: int, count: int, archive: list[archive_module.Entry]) -> None:
    """Дела по живой записи — столько, сколько нужно тесту про пачку.

    Отрезки настоящие: они режут живой текст, а не выдуманный. Дела заводятся
    напрямую, потому что тест здесь про **команду**, а не про разметку.
    """
    text = archive_module.entry(archive, LIVE_ENTRY).text
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for number in range(count):
                start = number * 10
                await connection.execute(
                    insert(Commitment).values(
                        source_entry_id=entry_id,
                        source_unit_id=None,
                        text=text[start : start + 40],
                        span_start=start,
                        span_end=start + 40,
                        state="open",
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_backfill_names_the_backlog_in_batches(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Команда кладёт дела в пачку, а не ходит по одному.

    Проверяется числом вызовов: тридцать дел при пачке в двадцать пять —
    два запроса. Вызов на дело съел бы суточный лимит модели за один прогон.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 30, archive)
    titler = Titler(FILM_TITLE)

    named = await maintenance.backfill_titles(
        model=titler, dry_run=False, recompute=False, limit=None
    )

    assert named == 30
    assert titler.calls == 2
    assert await _commitment_titles() == [FILM_TITLE] * 30


@pytest.mark.integration
async def test_backfill_dry_run_changes_nothing(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 3, archive)
    titler = Titler(FILM_TITLE)

    await maintenance.backfill_titles(model=titler, dry_run=True, recompute=False, limit=None)

    assert titler.calls == 0
    assert await _commitment_titles() == [None, None, None]


@pytest.mark.integration
async def test_backfill_leaves_existing_titles_alone(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """По умолчанию считаются только пустые: повтор команды не стоит квоты."""
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 2, archive)
    await maintenance.backfill_titles(
        model=Titler(FILM_TITLE), dry_run=False, recompute=False, limit=None
    )

    again = Titler("Другое название")
    named = await maintenance.backfill_titles(
        model=again, dry_run=False, recompute=False, limit=None
    )

    assert named == 0
    assert again.calls == 0
    assert await _commitment_titles() == [FILM_TITLE, FILM_TITLE]


@pytest.mark.integration
async def test_backfill_recomputes_when_asked(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Смена модели иначе оставила бы старые названия навсегда.

    Ровно та мина, из-за которой Задачу 4 откладывали: команда, умеющая только
    заполнять пустое, закрепляет в базе результат первой попавшейся модели.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 2, archive)
    await maintenance.backfill_titles(
        model=Titler(FILM_TITLE), dry_run=False, recompute=False, limit=None
    )

    named = await maintenance.backfill_titles(
        model=Titler("Посмотреть фильм"), dry_run=False, recompute=True, limit=None
    )

    assert named == 2
    assert await _commitment_titles() == ["Посмотреть фильм", "Посмотреть фильм"]


@pytest.mark.integration
async def test_backfill_respects_the_limit(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """`--limit` нужен, чтобы разложить прогон по дням: лимит модели — двадцать.

    Заодно закрепляется порядок: берутся **новые**, а не старые. Разметка зовёт
    то же чтение после каждой записи, и назвать она обязана то, что владелец
    только что сказал, а не хвост архива.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 5, archive)

    named = await maintenance.backfill_titles(
        model=Titler(FILM_TITLE), dry_run=False, recompute=False, limit=2
    )

    assert named == 2
    assert await _commitment_titles() == [None, None, None, FILM_TITLE, FILM_TITLE]


@pytest.mark.integration
async def test_a_closed_deed_is_not_worth_a_title(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Закрытое дело в сводке не показывается — платить за его название незачем."""
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 2, archive)
    async with session_scope() as session:
        closed = await session.scalar(select(Commitment.id).order_by(Commitment.id).limit(1))
        await session.execute(
            update(Commitment)
            .where(Commitment.id == closed)
            .values(state="done", state_changed_at=datetime.now(UTC))
        )

    named = await maintenance.backfill_titles(
        model=Titler(FILM_TITLE), dry_run=False, recompute=False, limit=None
    )

    assert named == 1


@pytest.mark.integration
async def test_titles_stay_quiet_while_cosmetics_are_held(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Под квотой названия замолкают первыми — как чистка (ADR-0015, п. 6).

    Разметка при этом идёт своим ходом: она в очереди «не трогается никогда».
    """
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _refused(status=429, at=datetime.now(UTC) - timedelta(minutes=1))

    titler = Titler(FILM_TITLE)
    await _annotate(entry_id, text, titler)

    assert titler.calls == 0
    assert await _commitment_titles() == [None, None]


async def _refused(*, status: int, at: datetime) -> None:
    """Отказ провайдера, записанный в журнал вызовов."""
    async with session_scope() as session:
        await session.execute(
            insert(LlmCall).values(
                created_at=at,
                provider="gemini",
                model="gemini-3.6-flash",
                tier="cheap",
                purpose="enrich",
                succeeded=False,
                status_code=status,
                error="429 quota",
            )
        )


@pytest.mark.integration
async def test_loop_without_a_title_still_shows_its_question(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Петля без названия — прежний вопрос, а не пустая строка."""
    text = archive_module.entry(archive, LIVE_ENTRY).text
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await loops.create(entry_id=entry_id, unit_id=None, question=text[:120])

    notifier = FakeNotifier()
    await service.tick(
        now=MORNING, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None
    )

    _, shown, _, _ = notifier.mornings[0]
    assert "Вот это по боту" in shown
    async with session_scope() as session:
        assert await session.scalar(select(Loop.title)) is None


# --- Предпросмотр: `--preview N` ---
#
# `--dry-run` печатает цену прогона и ни одного названия — то есть смету,
# а не предпросмотр. Ради результата ключ и был нужен: семьдесят названий
# лягут в базу, и `--recompute` стоит ещё четырёх вызовов. Поэтому здесь
# отдельный ключ, который **зовёт модель по-настоящему** и не пишет ничего.
#
# Модель по-прежнему заглушка: утверждения ниже про путь, про пачку и про то,
# что видит владелец, — а не про качество названия.


def test_preview_without_a_number_costs_one_call() -> None:
    """`--preview` без числа — десять дел, то есть один вызов.

    Проверяется на разборе аргументов, а не на константе: значение по умолчанию
    живёт в argparse, и «десять» в другом месте ничего не доказывало бы.
    """
    parsed = maintenance.build_parser().parse_args(["backfill-titles", "--preview"])

    assert parsed.preview == 10


def test_preview_and_dry_run_do_not_go_together() -> None:
    """Ключи отвечают на разные вопросы, и вместе они противоречат друг другу.

    `--dry-run` обещает не звать модель, `--preview` зовёт её обязательно.
    Молчаливое «один победил другого» здесь хуже отказа: владелец решил бы,
    что смету он тоже увидел.
    """
    with pytest.raises(SystemExit):
        maintenance.build_parser().parse_args(["backfill-titles", "--preview", "--dry-run"])


@pytest.mark.integration
async def test_preview_shows_the_title_next_to_the_span(
    migrated_database: str,
    archive: list[archive_module.Entry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Главное утверждение ключа: видно, **что изменилось**, а не только итог.

    Отрезок и название стоят друг под другом — иначе это снова смета.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 3, archive)
    titler = Titler(FILM_TITLE)

    await maintenance.backfill_titles(
        model=titler, dry_run=False, recompute=False, limit=None, preview=3
    )

    shown = capsys.readouterr().out
    assert "было:  Вот это по боту" in shown
    assert f"стало: {FILM_TITLE}" in shown
    # Одна пачка — один вызов: предпросмотр не имеет права стоить дороже.
    assert titler.calls == 1


@pytest.mark.integration
async def test_preview_writes_nothing_to_the_database(
    migrated_database: str, archive: list[archive_module.Entry]
) -> None:
    """Предпросмотр показывает и уходит: в базе после него ровно то же, что было."""
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 3, archive)

    await maintenance.backfill_titles(
        model=Titler(FILM_TITLE), dry_run=False, recompute=False, limit=None, preview=3
    )

    assert await _commitment_titles() == [None, None, None]


@pytest.mark.integration
async def test_preview_takes_one_batch_however_many_were_asked(
    migrated_database: str,
    archive: list[archive_module.Entry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Больше пачки не берётся: суточный лимит модели — двадцать запросов.

    Ключ обещает «одна пачка», и число сверх потолка обязано упереться в него,
    а не превратиться в четыре тихих вызова.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 30, archive)
    titler = Titler(FILM_TITLE)

    await maintenance.backfill_titles(
        model=titler, dry_run=False, recompute=False, limit=None, preview=30
    )

    assert titler.calls == 1
    assert capsys.readouterr().out.count("стало: ") == titles.BATCH_LIMIT


@pytest.mark.integration
async def test_preview_says_why_the_guard_refused(
    migrated_database: str,
    archive: list[archive_module.Entry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Отвергнутое название видно вместе с причиной, а не пропадает молча.

    Иначе владелец гадает, почему у дела остался отрезок: в базу такое название
    не попадёт и при настоящем прогоне, и знать об этом он должен здесь.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 2, archive)

    await maintenance.backfill_titles(
        model=Titler("Досмотреть фильм за 5 дней"),
        dry_run=False,
        recompute=False,
        limit=None,
        preview=2,
    )

    shown = capsys.readouterr().out
    assert "Досмотреть фильм за 5 дней" in shown
    assert "числа, которых не было во входе" in shown
    assert await _commitment_titles() == [None, None]


@pytest.mark.integration
async def test_preview_shows_the_deed_the_model_skipped(
    migrated_database: str,
    archive: list[archive_module.Entry],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Дело, про которое модель не сказала ничего, из предпросмотра не исчезает.

    Пропавшая строка читалась бы как «названо», а это ровно наоборот.
    """
    entry_id = await _live_entry(migrated_database, archive, LIVE_ENTRY)
    await _deeds(migrated_database, entry_id, 2, archive)

    await maintenance.backfill_titles(
        model=Titler(answer={}), dry_run=False, recompute=False, limit=None, preview=2
    )

    shown = capsys.readouterr().out
    assert shown.count("было:  ") == 2
    assert "модель не ответила" in shown
