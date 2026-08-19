"""Состояние системы: то, что нужно знать с телефона.

Отвечает на один вопрос — «всё ли работает». Главное здесь не цифры, а то,
что **бот и воркер это разные процессы**: бот может бодро отвечать «Принял»,
когда расшифровывать уже некому. Снаружи такая система выглядит исправной,
и заметить поломку можно только по тому, что расшифровки не приходят —
то есть через несколько часов и по случайности.

Живость воркера определяется по пульсу — минутному расписанию, которое ничего
не делает (см. `queue.py`). Считать её по последней обработанной записи нельзя:
ночью заданий нет вовсе, и спящий воркер неотличим от мёртвого.

Читает pgqueuer сырым SQL: его таблицы описаны библиотекой, а не нашими
моделями (ADR-0008), и заводить их отражение в ORM ради трёх счётчиков незачем.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from brain.core import enrich
from brain.core.memory import repository
from brain.core.retrieval import repository as retrieval
from brain.core.scheduler.queue import HEARTBEAT, HEARTBEAT_STALE_MINUTES
from brain.storage.db import session_scope
from brain.storage.models.annotation import AnnotationState
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.reminder import Reminder
from brain.storage.models.transcript import Transcript


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    """Жив ли воркер и когда о себе напоминал."""

    last_beat: datetime | None
    stale_after: timedelta

    @property
    def alive(self) -> bool:
        if self.last_beat is None:
            return False
        return datetime.now(UTC) - self.last_beat < self.stale_after

    @property
    def silent_for(self) -> timedelta | None:
        if self.last_beat is None:
            return None
        return datetime.now(UTC) - self.last_beat


@dataclass(frozen=True, slots=True)
class NextReminder:
    """Ближайшее из того, о чём система обещала напомнить."""

    fire_at: datetime
    text: str


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """Срез состояния на момент запроса."""

    worker: WorkerStatus
    entries: int
    voice_without_transcript: int
    entries_without_index: int
    # Не размечено и ждёт вечернего разбора. Две разные беды: первая означает,
    # что разметка отстаёт или лежит, вторая — что модель сомневается, и это
    # нормальная работа, а не поломка.
    entries_without_annotation: int
    waiting_for_review: int
    trigrams_work: bool
    jobs_queued: int
    jobs_failed: int
    next_reminder: NextReminder | None
    private_mode: bool


def _waiting(counts: dict[AnnotationState, int]) -> int:
    """Сколько записей ждут вечернего разбора.

    Это «две гипотезы» плюс «не понял» — оба состояния означают, что разметка
    сохранена, но спорна (vision, «модель подтверждений»). Складываются они
    намеренно: для владельца это одна очередь, а не две.
    """
    return counts.get(AnnotationState.TWO_HYPOTHESES, 0) + counts.get(AnnotationState.UNCLEAR, 0)


async def collect() -> SystemStatus:
    """Собрать состояние. Один проход по базе, никаких внешних вызовов."""
    async with session_scope() as session:
        entries = await session.scalar(select(func.count()).select_from(Entry)) or 0

        voice_without_transcript = (
            await session.scalar(
                select(func.count())
                .select_from(Entry)
                .where(
                    Entry.kind == EntryKind.VOICE,
                    ~select(Transcript.id).where(Transcript.source_entry_id == Entry.id).exists(),
                )
            )
            or 0
        )

        # `failed` — это задания, исчерпавшие попытки и оставленные в очереди
        # (`on_failure="hold"`). Они ждут `pgq requeue`, а не исчезают.
        queue = (
            await session.execute(
                text(
                    "SELECT "
                    "  count(*) FILTER (WHERE status IN ('queued', 'picked')) AS active, "
                    "  count(*) FILTER (WHERE status = 'failed') AS failed "
                    "FROM pgqueuer"
                )
            )
        ).one()

        # Работают ли триграммы на кириллице. Не «настроено ли что-то»,
        # а настоящая проверка вызовом — по тому же правилу, по которому
        # доступность модели проверяется вызовом, а не списком моделей.
        #
        # Под локалью базы `C` pg_trgm не считает кириллицу буквами вовсе
        # и возвращает пустой массив триграмм. Третий канал поиска при этом
        # молчит, а всё остальное работает: система выглядит здоровой.
        # Именно так и было до 18 августа 2026.
        trigrams = await session.scalar(text("SELECT array_length(show_trgm('кошка'), 1)"))

        last_beat = await session.scalar(
            text("SELECT last_run FROM pgqueuer_schedules WHERE entrypoint = :name"),
            {"name": HEARTBEAT},
        )

        upcoming = (
            await session.execute(
                select(Reminder.fire_at, Reminder.text)
                .where(Reminder.fired_at.is_(None))
                .order_by(Reminder.fire_at)
                .limit(1)
            )
        ).first()

    return SystemStatus(
        worker=WorkerStatus(
            last_beat=last_beat,
            stale_after=timedelta(minutes=HEARTBEAT_STALE_MINUTES),
        ),
        entries=int(entries),
        voice_without_transcript=int(voice_without_transcript),
        entries_without_index=len(await retrieval.entries_needing_index()),
        entries_without_annotation=await enrich.unannotated_count(),
        waiting_for_review=_waiting(await enrich.counts_by_state()),
        trigrams_work=bool(trigrams),
        jobs_queued=int(queue.active),
        jobs_failed=int(queue.failed),
        next_reminder=(
            None if upcoming is None else NextReminder(fire_at=upcoming[0], text=upcoming[1])
        ),
        private_mode=await repository.is_private_mode(),
    )
