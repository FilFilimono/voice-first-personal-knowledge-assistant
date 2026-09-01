"""Что должно работать — на живой базе, живом архиве и настоящих векторах:

1. запись при приёме получает связь с прежней записью, и приходит она
   отдельным беззвучным сообщением
2. связь не показывает саму запись и не показывает её же соседний чанк
3. приватная запись связей не получает — вектора у неё нет (инвариант 5)
4. запись, ставшая приватной задним числом, целью связи не бывает
5. `LINKS_ENABLED=false` — ни одного сообщения, при том что индексация идёт
6. `just reindex` не рассылает связи по всему архиву
7. отказ связи не ломает индексацию: вектора остаются посчитанными
8. связей показывается не больше, чем разрешено настройкой
9. цель связи — размышление, а не задача, и это меняется настройкой
10. `RECALL_WRITTEN=false` — модель не зовётся ни разу, фразу пишет шаблон
11. `RECALL_WRITTEN=true` — первую строку пишет модель, цитаты остаются кодом

**Вход системы здесь — `jobs.index`, а не `links.related`.** Связь считается
в задании индексации, и задание это зовётся на каждой записи. Тест от функции
проверил бы SQL и промолчал бы о том, доходит ли сообщение до владельца, —
ровно так в проекте месяц не работала смена вопроса у петли (`loops.is_stuck`
была написана, покрыта тестом и не вызывалась из `src/` ни разу).

**Данные — весь архив целиком, из живой базы.** Вектора настоящие, посчитаны
той самой моделью и лежат в игнорируемом git'ом кэше рядом (`tests/archive.py`).
Заглушка с хэшом от текста тут не годится вовсе: близость на хэше не значит
ничего, а связь — это и есть близость.

Тесты привязаны к номерам записей, а не к их тексту: текст живёт в базе,
и переносить его сюда значило бы вернуть архив в git (ADR-0002).

Числа в комментариях ниже — из прогона архива 01.09.2026, запросом к базе
по уже посчитанным векторам. Ноль вызовов модели.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings
from brain.core.present import recall
from brain.core.retrieval.chunking import split
from brain.core.retrieval.indexing import index_entry
from brain.core.scheduler import jobs
from brain.storage.db import session_scope
from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.chunk import Chunk
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.override import EntryOverride, OverrideAction
from tests import archive as archive_module
from tests.fakes import FakeNotifier, RecordedEmbedder, ScriptedModel

pytestmark = pytest.mark.integration

CHAT_ID = 111
TIMEZONE = "Europe/Rome"

# Момент приёма. Всё, что лежит в архиве, старше суток относительно него,
# то есть возрастной зазор снят для всего архива разом — и правило «кандидат
# старше на сутки» тут ничего не отбирает само по себе.
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

# --- Записи, на которых держатся проверки ---
#
# Запись 63 — размышление про доработку бота. Её кандидаты по близости
# (прогон 01.09.2026, порог 0.82, весь архив):
#
#       2  0.8771  задача      53  0.8604  задача      25  0.8562  мысль
#       1  0.8757  задача       9  0.8604  задача      21  0.8541  мысль
#      66  0.8645  петля       60  0.8575  задача      31  0.8508  мысль
#
# То есть ближе всего к ней — два почти одинаковых напоминания, и на них
# фраза «а кстати, что ты думал про…» не указывает ни при каком пороге.
# Ради этого и заведено правило про тип цели.
THINKER = 63
NEAREST_TASKS = (2, 1)
NEAREST_THOUGHTS = (25, 21)

# Запись 67 — три чанка. Её собственные чанки похожи друг на друга на 0.8892,
# а лучший чужой кандидат (34) — на 0.8443. Значит, без правила «ни сама
# запись, ни её соседний чанк» первой связью этой записи была бы она сама,
# и заметить это можно только на настоящих векторах.
MULTI_CHUNK = 67
MULTI_CHUNK_BEST = 34

# Типы, которые тест ставит сам. Живая разметка сюда не переносится нарочно:
# правило про тип цели проверяется тем, что мы знаем тип каждой цели, а не тем,
# что модель когда-то что-то ответила.
#
# Размышлений здесь четыре, а показывается две: без запаса потолок `LINKS_MAX`
# ничего бы не отрезал и тест на него был бы зелёным при любом значении.
TYPES: dict[int, AnnotationType] = {
    1: AnnotationType.TASK,
    2: AnnotationType.TASK,
    25: AnnotationType.THOUGHT,
    21: AnnotationType.THOUGHT,
    31: AnnotationType.THOUGHT,
    41: AnnotationType.THOUGHT,
    MULTI_CHUNK: AnnotationType.THOUGHT,
    MULTI_CHUNK_BEST: AnnotationType.THOUGHT,
}


# --- Приём записи целиком: архив в базе, вектора посчитаны, задание выполнено ---

Receive = Callable[..., Awaitable[FakeNotifier]]


@pytest.fixture
async def receive(
    migrated_database: str, archive: list[archive_module.Entry], monkeypatch: pytest.MonkeyPatch
) -> Receive:
    """Принять запись так, как её принимает воркер, и вернуть, что он сказал.

    Весь архив кладётся в одноразовую базу и индексируется настоящим кодом,
    кроме принимаемой записи: её индексирует само задание — иначе проверялось бы
    не задание, а фикстура.

    Возраст принимаемой записи — `now`, у остальных настоящий. Подменить его
    после вставки нельзя: `entries` только на вставку, UPDATE отозван триггером
    (инвариант 3).
    """

    async def _receive(
        entry_id: int,
        *,
        now: datetime = NOW,
        enabled: bool = True,
        written: bool = False,
        model: ScriptedModel | None = None,
        notifier: FakeNotifier | None = None,
        private_source: bool = False,
        private_targets: tuple[int, ...] = (),
        stale: bool = False,
    ) -> FakeNotifier:
        vectors = await archive_module.vectors(
            documents=[chunk for entry in archive for chunk in split(entry.text)], queries=[]
        )
        if vectors is None:
            pytest.skip(archive_module.NO_VECTORS)

        embedder = RecordedEmbedder(vectors, get_settings().embedding_model)
        # `stale` — это `just reindex`: та же запись, то же задание, но приём
        # был давно. Отличить пересчёт от приёма можно только по возрасту.
        received_at = now - timedelta(days=3) if stale else now

        await _fill(
            migrated_database,
            archive,
            fresh=entry_id,
            received_at=received_at,
            private_source=private_source,
        )
        for entry in archive:
            if entry.id != entry_id:
                await index_entry(embedder, entry_id=entry.id)
        await _annotate()
        await _make_private(private_targets)

        monkeypatch.setenv("LINKS_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("RECALL_WRITTEN", "true" if written else "false")
        get_settings.cache_clear()

        told = notifier if notifier is not None else FakeNotifier()
        await jobs.index(
            entry_id=entry_id,
            embedder=embedder,
            notifier=told,
            model=model if model is not None else ScriptedModel([{}]),
        )
        return told

    return _receive


async def _fill(
    url: str,
    archive: list[archive_module.Entry],
    *,
    fresh: int,
    received_at: datetime,
    private_source: bool,
) -> None:
    """Живой архив в одноразовой базе. Принимаемая запись — с моментом приёма."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for entry in archive:
                await connection.execute(
                    insert(Entry).values(
                        id=entry.id,
                        source=EntrySource.TELEGRAM,
                        kind=EntryKind(entry.kind),
                        raw_text=entry.text,
                        created_at=received_at
                        if entry.id == fresh
                        else datetime.fromisoformat(entry.created_at),
                        is_private=private_source and entry.id == fresh,
                        timezone=TIMEZONE,
                        dedup_key=f"archive:{entry.id}",
                        # Чат нужен связи: сообщение уходит туда же, куда ушло
                        # подтверждение приёма.
                        meta={"chat_id": CHAT_ID, "ack_message_id": 1},
                    )
                )
            await connection.execute(
                text("SELECT setval('entries_id_seq', (SELECT max(id) FROM entries))")
            )
    finally:
        await engine.dispose()


async def _annotate() -> None:
    """Разметка целей: чем считается запись, на которую связь может указать."""
    async with session_scope() as session:
        for entry_id, kind in TYPES.items():
            await session.execute(
                insert(Annotation).values(
                    source_entry_id=entry_id,
                    type=kind,
                    state=AnnotationState.SURE,
                    entities=[],
                    provider="test",
                    model="test",
                )
            )


async def _make_private(entry_ids: tuple[int, ...]) -> None:
    """«Сделать приватной» задним числом — вектор при этом уже посчитан.

    Именно так проверяется правило приватности у цели: если бы тест прятал
    запись до индексации, он проверял бы отсутствие вектора, а не условие
    в запросе. Оно и должно ловить тот день, когда вектор появится.
    """
    async with session_scope() as session:
        for entry_id in entry_ids:
            await session.execute(
                insert(EntryOverride).values(
                    source_entry_id=entry_id, action=OverrideAction.MAKE_PRIVATE, payload={}
                )
            )


async def _chunks_with_vectors(entry_id: int) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).where(
                    Chunk.source_entry_id == entry_id, Chunk.embedding.is_not(None)
                )
            )
            or 0
        )


def _said(notifier: FakeNotifier) -> str:
    """Единственное сказанное про связь. Пусто — значит смолчали."""
    return notifier.quiet[0][1] if notifier.quiet else ""


def _mentions(said: str, archive: list[archive_module.Entry], entry_id: int) -> bool:
    """Стоит ли в сообщении цитата этой записи. Сравнение — тем же обрезком."""
    return recall.snippet(archive_module.entry(archive, entry_id).text) in said


# --- Связь доходит до владельца ---


async def test_a_fresh_entry_gets_a_link_to_an_older_one(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Главное утверждение блока: запись при приёме получает повод вспомнить."""
    notifier = await receive(THINKER)

    said = _said(notifier)
    assert said, "связь не показана вовсе"
    assert _mentions(said, archive, NEAREST_THOUGHTS[0])


async def test_the_link_comes_as_a_separate_quiet_message(receive: Receive) -> None:
    """Отдельным и беззвучным: инициативное касание не требует внимания сейчас.

    Отдельным — потому что подтверждение приёма в этот момент занято чисткой
    и разметкой, и дописать в него связь значило бы драться за одно сообщение
    с двумя другими заданиями.
    """
    notifier = await receive(THINKER)

    assert len(notifier.quiet) == 1
    assert notifier.sent == []
    assert notifier.edited == []


async def test_every_link_carries_a_date(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Без даты «перекликается» ничем не отличается от «похоже написано»."""
    said = _said(await receive(THINKER))

    when = datetime.fromisoformat(archive_module.entry(archive, NEAREST_THOUGHTS[0]).created_at)
    assert f"{when:%d.%m.%Y}" in said


# --- Сама запись и её соседний чанк ---


async def test_a_link_never_points_at_the_entry_itself(
    receive: Receive, archive: list[archive_module.Entry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Самая очевидная и самая незаметная ошибка этого механизма.

    Запись 67 разрезана на три чанка, и её собственные чанки похожи друг
    на друга сильнее (0.8892), чем на что-либо чужое (0.8443). Значит, без
    правила «ни сама запись, ни её соседний чанк» первой связью здесь была бы
    она сама — и на выдуманных векторах это не всплыло бы никогда.

    **Возрастной зазор снят намеренно.** С суточным зазором запись отсекает
    саму себя побочным действием другого правила, и мутация — убрать условие
    `source_entry_id != entry_id` — оставляла тест зелёным (проверено 01.09.2026).
    То есть проверялся зазор, а не правило. При `LINKS_MIN_AGE_DAYS=0` порог
    возраста пропускает саму запись, и утверждение держится ровно на том
    условии, ради которого написано.
    """
    monkeypatch.setenv("LINKS_MIN_AGE_DAYS", "0")

    said = _said(await receive(MULTI_CHUNK))

    assert said, "у записи из трёх чанков не нашлось ни одной чужой связи"
    assert not _mentions(said, archive, MULTI_CHUNK), "связь показала саму запись"
    assert _mentions(said, archive, MULTI_CHUNK_BEST)


# --- Приватность, инвариант 5 ---


async def test_a_private_entry_gets_no_links(receive: Receive) -> None:
    """У приватной записи вектора нет, значит спрашивать не о чем.

    Проверяется путём, а не вызовом: приватность отсекается в индексации,
    и связь молчит следствием. Следствия ломаются тише правил, поэтому
    утверждение стоит отдельным тестом.
    """
    notifier = await receive(THINKER, private_source=True)

    assert notifier.quiet == []
    # Чанки при этом есть: приватная запись находится словами (ADR-0002).
    assert await _chunks_with_vectors(THINKER) == 0


async def test_an_entry_made_private_later_is_never_a_link_target(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Приватность спрашивается **сейчас**, а не колонкой на момент индексации.

    Вектор у цели к этому моменту уже посчитан и лежит в базе: владелец нажал
    «сделать приватной» после приёма. Механизм, полагающийся на «у приватных
    и так нет вектора», сломался бы здесь молча.
    """
    notifier = await receive(THINKER, private_targets=(NEAREST_THOUGHTS[0],))
    said = _said(notifier)

    assert not _mentions(said, archive, NEAREST_THOUGHTS[0])
    assert _mentions(said, archive, NEAREST_THOUGHTS[1]), "связь пропала вместе с приватной целью"


# --- Выключатель ---


async def test_nothing_is_said_while_the_setting_is_off(receive: Receive) -> None:
    """Механизм на пути приёма, включённый молча, — то, из-за чего блок откатывали.

    Настройка выключена по умолчанию (решение владельца 01.09.2026), и это
    утверждение про поведение, а не про строку в `config.py`: индексация идёт,
    вектора считаются, сообщений нет.
    """
    notifier = await receive(THINKER, enabled=False)

    assert notifier.quiet == []
    assert await _chunks_with_vectors(THINKER) > 0


# --- Перепрогон индекса ---


async def test_reindex_does_not_send_links_over_the_whole_archive(receive: Receive) -> None:
    """`just reindex` зовёт ровно это задание — по каждой записи архива.

    Отличить пересчёт от приёма можно только по возрасту записи, и это условие
    и есть защита. Без него одна команда обслуживания превратилась бы в 85
    сообщений подряд.
    """
    notifier = await receive(THINKER, stale=True)

    assert notifier.quiet == []
    assert await _chunks_with_vectors(THINKER) > 0


# --- Отказ связи ---


class RefusingNotifier(FakeNotifier):
    """Канал, который лёг ровно на беззвучном сообщении."""

    async def send_quietly(self, chat_id: int, text: str) -> int:
        raise RuntimeError("Telegram не ответил")


async def test_a_broken_link_does_not_break_indexing(receive: Receive) -> None:
    """Связь дешевле потерять, чем переиндексировать запись.

    Исключение отсюда ушло бы наверх, задание попало бы в ретраи — и пересчитало
    вектора, уже посчитанные и оплаченные, ради косметического сообщения.
    """
    await receive(THINKER, notifier=RefusingNotifier())

    assert await _chunks_with_vectors(THINKER) > 0


# --- Сколько связей и на что они указывают ---


async def test_no_more_links_than_the_setting_allows(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Двойка из `vision.md` §5.4, и потолок обязан отрезать.

    Годных целей у этой записи четыре (см. `TYPES`), показывается две — значит,
    третья по близости в сообщение не попала. Без этого утверждения тест был бы
    зелёным при любом значении настройки.
    """
    said = _said(await receive(THINKER))

    # Первая строка — фраза, дальше по строке на связь.
    assert len(said.split("\n\n")) == 1 + get_settings().links_max
    assert not _mentions(said, archive, 31), "третья по близости цель обрезана не была"


async def test_a_link_points_at_thinking_not_at_the_nearest_task(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """«А кстати, что ты думал про…» — вопрос к размышлению, не к напоминанию.

    Ближайшие к записи 63 — две почти одинаковых задачи (0.8771 и 0.8757),
    и они обходят размышления (0.8562 и 0.8541) по близости. Правило про тип
    цели тем и проверяется, что оно спорит с порядком по косинусу.
    """
    said = _said(await receive(THINKER))

    for entry_id in NEAREST_TASKS:
        assert not _mentions(said, archive, entry_id), f"связь указала на задачу {entry_id}"
    for entry_id in NEAREST_THOUGHTS:
        assert _mentions(said, archive, entry_id)


async def test_the_type_rule_is_a_setting(
    receive: Receive, archive: list[archive_module.Entry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снятое правило возвращает порядок по близости, а не ломает связь."""
    monkeypatch.setenv("LINKS_TARGET_THINKING", "false")

    said = _said(await receive(THINKER))

    assert _mentions(said, archive, NEAREST_TASKS[0])


# --- Кто пишет фразу ---


async def test_the_model_is_not_called_while_recall_written_is_off(receive: Receive) -> None:
    """Связь срабатывает на каждой записи, и вызов модели на ней стоил бы суток.

    Бесплатный тир — 20 запросов в сутки на модель, при живом темпе 9.4 записи
    в день. Поэтому по умолчанию фразу собирает шаблон кода, и модель молчит.
    """
    model = ScriptedModel([{"phrase": "модель не должна была писать"}])

    said = _said(await receive(THINKER, model=model))

    assert model.calls == 0
    assert recall.HEADLINE in said


async def test_the_model_writes_the_lead_when_the_flag_is_on(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Ветка за флагом обязана работать, иначе это код, который не вызывается.

    Ровно тем почерком в проекте потерялась `loops.is_stuck`: написана, покрыта
    тестом, не вызвана из `src/` ни разу. Флаг здесь включается тестом.
    """
    phrase = "Тот же разбор бота, о котором вы думали раньше"
    model = ScriptedModel([{"phrase": phrase}])

    said = _said(await receive(THINKER, written=True, model=model))

    assert model.calls == 1
    assert phrase in said
    assert recall.HEADLINE not in said
    # Цитаты и даты ставит код при любом раскладе: модель пишет одну строку.
    assert _mentions(said, archive, NEAREST_THOUGHTS[0])


async def test_a_refused_phrase_falls_back_to_the_template(
    receive: Receive, archive: list[archive_module.Entry]
) -> None:
    """Гвард отверг — уходит шаблон, а не пустая строка и не вторая попытка."""
    model = ScriptedModel([{"phrase": "вы так и не вернулись к этому"}])

    said = _said(await receive(THINKER, written=True, model=model))

    assert recall.HEADLINE in said
    assert "так и не вернулись" not in said
    assert _mentions(said, archive, NEAREST_THOUGHTS[0])
