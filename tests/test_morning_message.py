"""Утреннее сообщение после переписывания 6 сентября 2026.

**Почему отдельный файл, а не строки в `test_digest_render`.** Проверяется
не рендер, а то, что получил владелец: `service.tick` → `FakeNotifier` → текст.
Разница здесь не педантизм. Строка «…и ещё 64» — та самая, которую владелец
назвал «самой ненужной хернёй», — **не была покрыта ни одним тестом вовсе**:
её удаление не уронило ни одной проверки из 777. Чистая функция рендера и
раньше делала ровно то, чего от неё хотели; вопрос был не в ней.

Три утверждения, по одному на каждый дефект из претензии владельца:

1. **счётчика нет** — при том что дел в базе больше, чем показывается. Иначе
   утверждение выполнялось бы само собой;
2. **срез по темам есть** — и он не число, а дверь: в нём стоит команда,
   которой тема разворачивается в свои записи;
3. **дела ротируются** — показанное вчера сегодня уступает место непоказанному.
   Именно этого не было: дела 3 и 5 приходили владельцу каждое утро с 21
   августа по 6 сентября, шестнадцать раз подряд, и видно это стало только
   запросом к журналу касаний.

**Про даты.** Момент передаётся в `service.tick` параметром, и весь путь —
сводка, дела, темы, раскачка — считает от него. Настоящие часы читает
единственное место (`touches.record_sent`, поле `sent_at`), и ни одно
утверждение здесь на него не смотрит. Поэтому прибитая дата тут безопасна,
и это проверено чтением кода, а не предположением (`CLAUDE.md`, правило часов).

Требует поднятого Postgres: `just up`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from brain.core.digest import service, touches
from brain.core.enrich import repository as enrichment
from brain.core.enrich.repository import NewTopic
from brain.core.enrich.units import MergedUnit
from brain.core.present.digest import MAX_DEEDS
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState, AnnotationType
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.touch import TouchSlot
from tests import archive as archive_module
from tests.fakes import FakeNotifier

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Rome"
CHAT_ID = 111

# 09:30 по Риму — внутри утреннего окна, тот же момент, что в соседних тестах.
MORNING = datetime(2026, 8, 20, 7, 30, tzinfo=UTC)
YESTERDAY = MORNING - timedelta(days=1)

# Сколько дел показывает утро. Число живёт в слое представления
# (`present.digest.MAX_DEEDS`), и повторять его тут числом нельзя: разъедется.

# Живые записи, из которых заводятся дела и темы. Берутся по номерам из архива
# (ADR-0002): выдуманная строка не ломает код так, как ломает живая речь.
DEED_ENTRIES = (53, 13, 21, 25, 47)


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


async def _entry(entries: list[archive_module.Entry], number: int, *, when: datetime) -> int:
    """Живая запись в базе. Сырым SQL — ради точной даты создания."""
    body = archive_module.entry(entries, number).text
    async with session_scope() as session:
        entry_id = await session.scalar(
            sql(
                "INSERT INTO entries "
                "(created_at, source, kind, raw_text, is_private, timezone, dedup_key, meta) "
                "VALUES (:created_at, 'telegram', 'text', :body, false, :tz, :key, '{}'::jsonb) "
                "RETURNING id"
            ),
            {"created_at": when, "body": body, "tz": TIMEZONE, "key": f"morning:{number}"},
        )
    return int(entry_id or 0)


async def _annotate(entry_id: int, body: str, topic: str) -> None:
    """Разметка записи рабочим путём: тема и дело заводятся ею, а не тестом.

    Сырым SQL это писать нельзя, и не из чистоплюйства: `entry_topics` требует
    `annotation_id`, дела выводятся из единиц типа «задача», и всё это лежит
    в одной транзакции (`enrich/repository.save_annotation`). Обойдя её, тест
    проверял бы состояние базы, которого рабочий путь не создаёт.
    """
    await enrichment.save_annotation(
        entry_id=entry_id,
        type_=AnnotationType.TASK,
        alternative_type=None,
        state=AnnotationState.SURE,
        entities=[],
        provider="test",
        model="test",
        existing_topic_ids=[],
        # Без вектора: тема нужна срезу и свежести, а не поиску похожих тем.
        new_topics=[NewTopic(name=topic, reason="тест", embedding=None, embedding_model=None)],
        units=[
            MergedUnit(
                start=0,
                end=len(body),
                type=AnnotationType.TASK,
                alternative_type=None,
                state=AnnotationState.SURE,
            )
        ],
        transcript_id=None,
        text=body,
    )


async def _many_deeds(entries: list[archive_module.Entry]) -> list[int]:
    """Дел заметно больше, чем показывается: иначе счётчику неоткуда взяться.

    Записи заводятся с разным возрастом и по двум темам — на этом же материале
    проверяется и ротация, и свежесть темы. Дело у записи одно: границы единиц
    здесь не предмет проверки, а лишний разрез добавил бы делам порядок,
    которого тест не задавал.
    """
    for shift, number in enumerate(DEED_ENTRIES):
        when = YESTERDAY - timedelta(days=shift)
        entry_id = await _entry(entries, number, when=when)
        body = archive_module.entry(entries, number).text
        await _annotate(entry_id, body, "книги" if shift % 2 else "разработка")

    async with session_scope() as session:
        rows = await session.scalars(
            select(Commitment.id).where(Commitment.state == CommitmentState.OPEN)
        )
    return [int(value) for value in rows.all()]


async def _sent(notifier: FakeNotifier) -> str:
    """Текст утреннего сообщения. Пусто — значит утро промолчало."""
    assert notifier.mornings, "утро не пришло — проверять нечего"
    return str(notifier.mornings[0][1])


async def _tick(notifier: FakeNotifier, now: datetime = MORNING) -> None:
    await service.tick(now=now, timezone=TIMEZONE, chat_id=CHAT_ID, notifier=notifier, model=None)


async def test_the_morning_never_counts_the_deeds_it_hides(
    archive: list[archive_module.Entry],
) -> None:
    """Счётчика «…и ещё N» в сообщении нет, хотя скрытые дела есть.

    Это не косметика. `vision.md` §7.0 п.5 запрещает счётчики прямым текстом,
    но держалась строка не на этом, а на том, что за многоточием лежали дела,
    о которых владелец забыл, и система не напомнила бы о них никогда.

    Проверяется на базе, где скрытых дел **больше нуля**: без этого утверждение
    выполнялось бы само собой и правку бы не удержало.
    """
    made = await _many_deeds(archive)
    assert len(made) > MAX_DEEDS, "скрытых дел нет — тест ничего не проверяет"

    notifier = FakeNotifier()
    await _tick(notifier)

    text = await _sent(notifier)
    assert "и ещё" not in text
    # Строк дела ровно столько, сколько показывается, и ни одной сверх:
    # приписка про скрытые была именно такой лишней строкой.
    numbered = [line for line in text.splitlines() if line[:1].isdigit()]
    assert len(numbered) == MAX_DEEDS


async def test_the_morning_opens_the_archive_by_topic(
    archive: list[archive_module.Entry],
) -> None:
    """На месте счётчика — срез по темам, и он дверь, а не число.

    Проверяется не только присутствие строки, но и то, что тема **называется
    командой**: список тем без способа их развернуть был бы тем же счётчиком,
    только в профиль.
    """
    await _many_deeds(archive)

    notifier = FakeNotifier()
    await _tick(notifier)

    text = await _sent(notifier)
    assert "В архиве:" in text
    assert "Разработка" in text
    assert "Книги" in text
    assert "/recall" in text


async def test_the_material_stands_above_and_the_question_below(
    archive: list[archive_module.Entry],
) -> None:
    """Порядок сообщения: сначала читать, потом делать, вопрос последним.

    Требование владельца 06.09.2026 дословно: «материал сверху → срез по темам
    → дела → вопрос раскачки в конце». Порядок проверяется позициями в строке,
    а не глазами: переставить блоки местами — правка одной строки в рендере,
    и заметить её было бы нечем.
    """
    await _many_deeds(archive)

    notifier = FakeNotifier()
    await _tick(notifier)

    text = await _sent(notifier)
    assert text.startswith("☀️ Утро.")
    assert text.index("В архиве:") < text.index("Висит:")
    if "💬" in text:
        assert text.index("Висит:") < text.index("💬"), "вопрос обязан быть последним"


async def test_deeds_shown_yesterday_give_way_to_the_ones_never_shown(
    archive: list[archive_module.Entry],
) -> None:
    """Ротация: показанное вчера уступает место непоказанному.

    **Это и есть претензия владельца, измеренная.** До правки отбор шёл
    по возрасту, и три старейших дела из шестидесяти семи приходили каждое
    утро — в рабочей базе дела 3 и 5 ушли так шестнадцать раз подряд.

    Утверждение выбрано точным, а не сильным: «список поменялся» было бы верно
    и при случайной перетасовке. Проверяется, что **все непоказанные дела
    пришли на второй день** — то есть очередь действительно движется. Дел
    в тесте пять, показывается три, поэтому на второй день два места занимают
    непоказанные, а третье неизбежно достаётся повтору: это и есть ротация,
    а не её нарушение.
    """
    made = await _many_deeds(archive)

    notifier = FakeNotifier()
    await _tick(notifier)
    first = set(notifier.mornings[0][2])
    assert len(first) == MAX_DEEDS

    # Журнал уже помнит показ — тем же путём, которым его пишет тик.
    assert await touches.deeds_shown(), "показ не записался: ротации не на чем стоять"

    # Второй день: слот вчерашнего дня занят, берём следующие сутки.
    later = FakeNotifier()
    await _tick(later, now=MORNING + timedelta(days=1))
    second = set(later.mornings[0][2])

    waiting = set(made) - first
    assert waiting, "непоказанных дел не осталось — очереди нет"
    assert waiting <= second, "непоказанные дела снова не пришли"
    assert second <= set(made)


async def test_the_morning_slot_is_recorded_once_a_day(
    archive: list[archive_module.Entry],
) -> None:
    """Переписанное утро не сломало занятие слота.

    Проверка стоит рядом с ротацией не случайно: ротация читает журнал касаний,
    и порядок «сначала занять слот, потом отправить» — то, на чём она держится.
    Разъедься они — дело показалось бы дважды или не показалось вовсе.
    """
    await _many_deeds(archive)

    notifier = FakeNotifier()
    await _tick(notifier)
    await _tick(notifier)

    assert len(notifier.mornings) == 1
    assert (await touches.of_day(MORNING.date())).keys() >= {TouchSlot.MORNING}
