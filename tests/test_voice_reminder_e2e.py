"""Сквозной путь голосового напоминания: аудио → расшифровка → напоминание → отправка.

Этого теста не хватало дважды, и дважды дыра проезжала мимо. Первый раз
разбор времени вообще не звали после расшифровки; второй раз позвали, но
проверяли на тексте, который написал автор кода — с цифрой. Живая речь
цифр не содержит: Gemini расшифровывает «через две минуты» словами.

Отсюда правило для этого файла: **тексты здесь — настоящие, из базы.**
Не «похожие на настоящие», а буквально те, что сказал владелец. Придумывать
их самому — это снова проверять своё представление о речи вместо речи.

Требует поднятого Postgres: `just up`.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain.config import get_settings
from brain.core import ingest
from brain.core.memory import repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.scheduler import jobs
from tests.archive import Entry, entry
from tests.fakes import FakeAudioSource, FakeNotifier, StubTranscriber

pytestmark = pytest.mark.integration

TIMEZONE = "Europe/Moscow"
CHAT_ID = 111
ACK_MESSAGE_ID = 21

# Настоящие голосовые владельца — по номерам записей: текст живёт в базе
# и в репозиторий не попадает (ADR-0002).
#
# 3 — «через 3 минуты», цифрой. Пережила прошлую починку и работала.
# 4 — «через две минуты», числительным словом. Не работала: парсер знал `\d+`,
#     а живая речь цифр не содержит. Ровно на ней дыра проехала второй раз.
SPOKEN_WITH_DIGIT = 3
SPOKEN_WITH_WORD = 4


@pytest.fixture(autouse=True)
def database(migrated_database: str) -> str:
    return migrated_database


@pytest.fixture
async def audio_dir(tmp_path: Path) -> AsyncGenerator[Path]:
    yield tmp_path


async def _capture_voice(audio_dir: Path) -> int:
    """Принять голосовое так же, как это делает бот."""
    async with capture_transaction() as uow:
        result = await ingest.capture_voice(
            uow,
            ingest.IncomingVoice(
                chat_id=CHAT_ID,
                message_id=20,
                ack_message_id=ACK_MESSAGE_ID,
                file_id="file-abc",
                duration_sec=12,
            ),
            is_private=False,
            timezone=TIMEZONE,
            audio_dir=audio_dir,
        )
    assert result.entry_id is not None
    return result.entry_id


async def _transcribe(entry_id: int, spoken: str, notifier: FakeNotifier) -> None:
    """Отработать задание расшифровки так же, как это делает воркер.

    Двумя заданиями, а не одним, потому что путь именно такой с Фазы 5:
    расшифровка сохраняет текст и ставит чистку, а показывает — либо чистка,
    либо срок ожидания (`show-transcript`), если она не успела. Здесь
    проверяются напоминания, а не чистка, поэтому проходится вторая ветка:
    модели в тестах нет, чистка не случится, и владелец видит дословный текст.
    """
    await jobs.transcribe(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=ACK_MESSAGE_ID,
        transcriber=StubTranscriber(spoken),
        audio_source=FakeAudioSource(),
        notifier=notifier,
        max_duration_sec=900,
    )
    await jobs.show_transcript(
        entry_id=entry_id,
        chat_id=CHAT_ID,
        ack_message_id=ACK_MESSAGE_ID,
        notifier=notifier,
    )


@pytest.mark.parametrize(
    ("spoken", "expected_delay"),
    [
        pytest.param(SPOKEN_WITH_DIGIT, timedelta(minutes=3), id="цифрой"),
        pytest.param(SPOKEN_WITH_WORD, timedelta(minutes=2), id="словом"),
    ],
)
async def test_voice_reminder_travels_the_whole_way(
    archive: list[Entry], audio_dir: Path, spoken: int, expected_delay: timedelta
) -> None:
    """Аудио → расшифровка → напоминание заведено → напоминание отправлено."""
    notifier = FakeNotifier()
    started = datetime.now(UTC)

    entry_id = await _capture_voice(audio_dir)
    await _transcribe(entry_id, entry(archive, spoken).text, notifier)

    # 1. Напоминание заведено и привязано к записи (инвариант 4).
    reminder = await repository.get_reminder(1)
    assert reminder is not None, "напоминание не создано"
    assert reminder.source_entry_id == entry_id

    # 2. Сработает тогда, когда обещано. Допуск в обе стороны: между началом
    # теста и разбором проходит немного времени, и оно ложится в задержку.
    assert abs((reminder.fire_at - started) - expected_delay) < timedelta(seconds=5)

    # 3. Владелец увидел обещание сразу, а не узнал о нём при срабатывании.
    _, _, shown = notifier.edited[0]
    assert "Напомню" in shown

    # 4. Напоминание доходит.
    await jobs.remind(reminder_id=reminder.id, chat_id=CHAT_ID, notifier=notifier)
    assert notifier.sent, "напоминание не отправлено"
    assert reminder.text in notifier.sent[0][1]


async def test_unparsed_time_is_not_stored_as_an_ordinary_thought(audio_dir: Path) -> None:
    """Увидел «напомни», не понял когда — скажи вслух.

    Молчаливое сохранение здесь хуже отказа: владелец уверен, что напоминание
    заведено, и узнаёт обратное, не дождавшись его. Именно так пропали
    и «через две минуты», и «через 3 минуты».
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(audio_dir)

    await _transcribe(entry_id, "напомни как-нибудь потом заглянуть в архив", notifier)

    assert await repository.get_reminder(1) is None

    _, _, shown = notifier.edited[0]
    assert "не понял, когда напомнить" in shown.lower()


async def test_voice_without_the_trigger_word_is_questioned(
    archive: list[Entry], audio_dir: Path
) -> None:
    """Голос — главный источник просьб без слова «напомни».

    Расшифровка записи 27 съела первую букву у «Напомни», и триггер за такое
    не цепляется. Голосовой путь обязан спросить так же, как текстовый:
    молча положить в архив — та самая дыра.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(audio_dir)

    await _transcribe(entry_id, entry(archive, 27).text, notifier)

    assert notifier.asked, "голосовой путь промолчал"
    _, _, shown, asked_about = notifier.asked[0]
    assert asked_about == entry_id
    assert "Похоже на напоминание" in shown
    assert "19:40" in shown

    # Вопрос — это вопрос: напоминание появится только после ответа.
    assert await repository.get_reminder(1) is None


async def test_ordinary_voice_note_says_nothing_about_reminders(audio_dir: Path) -> None:
    """Обычная мысль остаётся обычной мыслью: ни обещаний, ни извинений."""
    notifier = FakeNotifier()
    entry_id = await _capture_voice(audio_dir)

    await _transcribe(entry_id, "завтра будет тяжёлый день, надо выспаться", notifier)

    assert await repository.get_reminder(1) is None

    _, _, shown = notifier.edited[0]
    assert "Напомню" not in shown
    assert "не понял" not in shown.lower()


async def test_a_spoken_reminder_offers_the_calendar(
    archive: list[Entry], audio_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Наговорённое «напомни» тоже даёт кнопку в календарь. Фаза 5, ADR-0016.

    **Этот тест написан после того, как дыра нашлась.** Кнопка была сделана
    в адаптере — там, где владелец печатает, — а голосовое приходит не туда:
    напоминание из расшифровки заводит воркер, и ветка `jobs._show` про
    календарь не знала. Голос при этом главный способ записи, то есть фаза
    работала бы на меньшей половине случаев и **молча** — ровно тем почерком,
    которым в проекте уже вели себя напоминания, работавшие только для текста.

    Проверяется отдельным списком заглушки, а не текстом сообщения: «показали
    расшифровку» и «показали расшифровку с кнопкой наружу» — разные
    утверждения, и по тексту они неотличимы.
    """
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "брайн.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "секрет")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "1//0gрефреш")
    monkeypatch.setenv("GOOGLE_CALENDAR_TARGET_ID", "brain@group.calendar.google.com")
    get_settings.cache_clear()

    notifier = FakeNotifier()
    entry_id = await _capture_voice(audio_dir)
    await _transcribe(entry_id, entry(archive, SPOKEN_WITH_DIGIT).text, notifier)

    assert notifier.calendar_offers, "кнопки в календарь нет у наговорённого напоминания"
    _, _, shown, offered_entry = notifier.calendar_offers[-1]
    assert offered_entry == entry_id
    assert "Напомню" in shown


async def test_an_unconfigured_calendar_leaves_the_spoken_reminder_alone(
    archive: list[Entry], audio_dir: Path
) -> None:
    """Календарь не настроен — расшифровка показывается как раньше, без кнопки.

    Ненастроенный календарь не имеет права менять то, что владелец видит
    после голосового: до Фазы 5 это работало, и обязано работать так же.
    """
    notifier = FakeNotifier()
    entry_id = await _capture_voice(audio_dir)
    await _transcribe(entry_id, entry(archive, SPOKEN_WITH_DIGIT).text, notifier)

    assert notifier.calendar_offers == []
    assert any("Напомню" in text for _, _, text in notifier.edited)
