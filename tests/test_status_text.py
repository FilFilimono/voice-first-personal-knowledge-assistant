"""Что должно работать:

1. мёртвый воркер виден первой строкой и **со временем молчания**: разница
   между «минуту назад перезапустили» и «лежит со вчера» — это разница между
   «подождать» и «чинить»
2. воркер, который ни разу не отметился, — тоже мёртвый, а не «неизвестно»
3. пустые разделы не занимают место: нечего показать — строки нет
4. упавшие задания приходят вместе с командой, которая их возвращает
5. напоминание показывается в таймзоне владельца, а не в UTC сервера
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from brain.adapters.telegram.status_text import render
from brain.core.status import NextReminder, SystemStatus, Touches, WorkerStatus
from brain.storage.models.touch import SilenceReason, Touch, TouchCategory, TouchSlot

TIMEZONE = "Europe/Moscow"
STALE_AFTER = timedelta(minutes=3)

# Касания по умолчанию: сводки сегодня ещё не было, бюджет цел, тишины нет.
# Большинство тестов здесь проверяют другое, и разбирать блок касаний им ни к
# чему; те, что проверяют его, передают своё.
NO_TOUCHES = Touches(
    morning=None,
    spent=0,
    budget=3,
    quiet_until=None,
    open_commitments=0,
    open_loops=0,
    auto_resolved=0,
)


def _status(
    *,
    silent_for: timedelta | None = timedelta(seconds=10),
    entries: int = 12,
    voice_without_transcript: int = 0,
    entries_without_index: int = 0,
    entries_without_annotation: int = 0,
    waiting_for_review: int = 0,
    trigrams_work: bool = True,
    jobs_queued: int = 0,
    jobs_failed: int = 0,
    next_reminder: NextReminder | None = None,
    private_mode: bool = False,
    # По умолчанию архив уже взрослый: большинство тестов проверяют не разгон,
    # а обычную работу, и блок «система разгоняется» им только мешал бы.
    topics: int = 9,
    archive_age_days: int = 60,
    touches: Touches | None = None,
) -> SystemStatus:
    last_beat = None if silent_for is None else datetime.now(UTC) - silent_for
    return SystemStatus(
        worker=WorkerStatus(last_beat=last_beat, stale_after=STALE_AFTER),
        entries=entries,
        voice_without_transcript=voice_without_transcript,
        entries_without_index=entries_without_index,
        entries_without_annotation=entries_without_annotation,
        waiting_for_review=waiting_for_review,
        trigrams_work=trigrams_work,
        jobs_queued=jobs_queued,
        jobs_failed=jobs_failed,
        next_reminder=next_reminder,
        private_mode=private_mode,
        topics=topics,
        archive_age_days=archive_age_days,
        touches=touches or NO_TOUCHES,
    )


def test_live_worker_is_the_first_line() -> None:
    report = render(_status(), timezone=TIMEZONE)

    assert report.splitlines()[0] == "✅ Воркер жив"


@pytest.mark.parametrize(
    ("silent", "expected"),
    [
        (timedelta(minutes=12), "12 мин"),
        (timedelta(hours=3), "3 ч"),
        (timedelta(days=2), "2 дн"),
    ],
)
def test_dead_worker_reports_how_long_it_has_been_silent(silent: timedelta, expected: str) -> None:
    report = render(_status(silent_for=silent), timezone=TIMEZONE)

    first = report.splitlines()[0]
    assert "🔴" in first
    assert expected in first
    assert "just worker" in first


def test_worker_that_never_reported_is_dead_not_unknown() -> None:
    """Воркер, не отметившийся ни разу, — это не запущенный воркер."""
    report = render(_status(silent_for=None), timezone=TIMEZONE)

    assert "ни разу не отмечался" in report
    assert "just worker" in report


def test_quiet_sections_are_omitted() -> None:
    report = render(_status(), timezone=TIMEZONE)

    assert "без расшифровки" not in report
    assert "Упало" not in report
    assert "Напоминаний нет" in report


def test_backlog_and_failures_are_shown_with_the_way_out() -> None:
    report = render(
        _status(voice_without_transcript=2, jobs_queued=3, jobs_failed=1),
        timezone=TIMEZONE,
    )

    assert "Голосовых без расшифровки: 2" in report
    assert "Заданий в очереди: 3" in report
    assert "Упало: 1" in report
    assert "just requeue" in report


def test_reminder_is_shown_in_the_owners_timezone() -> None:
    # 07:30 UTC — это 10:30 в Москве.
    reminder = NextReminder(
        fire_at=datetime(2026, 8, 17, 7, 30, tzinfo=UTC), text="позвонить в банк"
    )

    report = render(_status(next_reminder=reminder), timezone=TIMEZONE)

    assert "17.08 в 10:30" in report
    assert "позвонить в банк" in report


def test_private_mode_is_visible() -> None:
    assert "🔒" in render(_status(private_mode=True), timezone=TIMEZONE)
    assert "🔓" in render(_status(private_mode=False), timezone=TIMEZONE)


def test_warming_up_block_explains_what_waits_for_content() -> None:
    """Молчащий механизм и сломанный снаружи неотличимы — поэтому объясняем."""
    report = render(_status(entries=39, topics=2, archive_age_days=4), timezone=TIMEZONE)

    assert "📊 Система пока разгоняется" in report
    assert "Записей: 39, тем: 2, архиву 4 дня." in report
    assert "• пересказ недели — нужна неделя записей, пока 4 дня" in report
    assert "• напоминания о старом — доставать пока нечего, архиву 4 дня" in report


def test_warming_up_says_nothing_about_links() -> None:
    """Про связи правды пока нет: порог из ADR-0006 пропускает 37% всех пар.

    Написать «пока рано» было бы неправдой — они не молчат, а сработают
    слишком часто. Молчим до тех пор, пока инструмент не выбран.
    """
    report = render(_status(entries=39, topics=2, archive_age_days=4), timezone=TIMEZONE)

    assert "связ" not in report.lower()
    assert "переклик" not in report.lower()


def test_grown_archive_has_no_warming_up_block() -> None:
    """Блок исчезает сам: разгон кончился — про него больше не пишем."""
    report = render(_status(entries=400, topics=20, archive_age_days=90), timezone=TIMEZONE)

    assert "разгоняется" not in report


def test_days_are_declined_like_russian_words() -> None:
    """«архиву 1 дней» читается как поломка, даже когда всё работает."""
    for days, expected in ((1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней")):
        report = render(_status(archive_age_days=days, topics=1), timezone=TIMEZONE)
        assert f"архиву {expected}" in report


# --- Касания: три состояния сводки вместо двух ---


def _touch(*, sent_at: datetime | None = None, reason: SilenceReason | None = None) -> Touch:
    return Touch(
        slot=TouchSlot.MORNING,
        category=TouchCategory.MORNING,
        local_date=date(2026, 8, 20),
        sent_at=sent_at,
        silence_reason=reason,
        payload={},
    )


def test_missing_summary_and_silent_summary_read_differently() -> None:
    """Три состояния, а не два, и это главное, ради чего журнал заведён.

    «Сводки ещё не было» означает, что до решения не дошли вовсе — например,
    воркер не работал в нужный час. «Промолчала» означает, что решение принято
    и всё в порядке. Свести их в одну строку значило бы вернуть ту самую
    неотличимость молчания от смерти, из-за которой прятались дыры Фаз 1–2.
    """
    absent = render(_status(), timezone=TIMEZONE)
    silent = render(
        _status(
            touches=Touches(
                morning=_touch(reason=SilenceReason.NOTHING_TO_SAY),
                spent=0,
                budget=3,
                quiet_until=None,
                open_commitments=0,
                open_loops=0,
                auto_resolved=0,
            )
        ),
        timezone=TIMEZONE,
    )

    assert "Сводки сегодня ещё не было" in absent
    assert "промолчала: нечего было сказать" in silent


def test_quiet_period_is_visible_with_the_way_out() -> None:
    """Тишина без способа её снять читается как поломка."""
    report = render(
        _status(
            touches=Touches(
                morning=_touch(reason=SilenceReason.QUIET),
                spent=0,
                budget=3,
                quiet_until=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
                open_commitments=0,
                open_loops=0,
                auto_resolved=0,
            )
        ),
        timezone=TIMEZONE,
    )

    assert "🔇 Молчу до" in report
    assert "/resume" in report


def test_auto_resolved_debt_is_answerable_but_never_pushed() -> None:
    """Про долг не сообщают уведомлением, но спросить владелец вправе.

    Это ровно граница принципа 2 из `vision.md` §7.0: запрещено сообщение,
    а не знание.
    """
    report = render(
        _status(
            touches=Touches(
                morning=None,
                spent=1,
                budget=3,
                quiet_until=None,
                open_commitments=4,
                open_loops=2,
                auto_resolved=3,
            )
        ),
        timezone=TIMEZONE,
    )

    assert "Зафиксировано само по сроку: 3" in report
    assert "Висит: дел 4, открытых вопросов 2" in report
    assert "Касаний сегодня: 1 из 3" in report
