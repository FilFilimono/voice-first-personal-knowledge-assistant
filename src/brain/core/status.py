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

from sqlalchemy import func, or_, select, text

import brain.core.quota as quota_counter
from brain.core import digest, enrich
from brain.core.llm.gemini import CHEAP
from brain.core.memory import repository
from brain.core.retrieval import repository as retrieval
from brain.core.retrieval.gemini import EMBEDDING
from brain.core.scheduler.queue import HEARTBEAT, HEARTBEAT_STALE_MINUTES
from brain.storage.db import session_scope
from brain.storage.models.entry import Entry, EntryKind
from brain.storage.models.reminder import Reminder
from brain.storage.models.topic import Topic
from brain.storage.models.touch import Touch, TouchSlot
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
class Touches:
    """Что сегодня было с касаниями — включая то, чего не было.

    Существует ровно затем, чтобы молчание было отличимо от смерти. Решение
    «нечего сказать — молчим» (`vision.md` §7.0) без этой строки означало бы,
    что владелец не может узнать, работает ли Фаза 4 вообще.
    """

    # Что случилось с утренней сводкой сегодня. `None` — момент ещё не настал
    # или воркер до него не дожил, и это разные беды: вторая видна по пульсу.
    morning: Touch | None
    spent: int
    budget: int
    quiet_until: datetime | None
    open_commitments: int
    open_loops: int
    # Сколько записей зафиксировалось само. Владельцу об этом не сообщают
    # (`vision.md` §7.0, принцип 2) — но спросить он вправе.
    auto_resolved: int


@dataclass(frozen=True, slots=True)
class Quota:
    """Расход запросов к провайдеру за текущие сутки — его сутки, не наши.

    Существует затем, чтобы в лимит не упирались вслепую. 20 августа 2026
    воркер встал из-за исчерпанной квоты эмбеддингов, и узнать заранее,
    сколько израсходовано, было неоткуда.
    """

    embedding: quota_counter.Usage
    enrich: quota_counter.Usage
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """Срез состояния на момент запроса."""

    worker: WorkerStatus
    entries: int
    voice_without_transcript: int
    # Расшифровок, до которых чистка не дошла. Отставание обязано быть видно
    # с телефона: без этой строки владелец узнаёт о вставшей чистке только
    # по тому, что в сообщениях снова появились «э» и «мм», — то есть заметит
    # не сразу и спутает с плохой работой модели, а не с отсутствием вызова.
    transcripts_without_cleanup: int
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
    # Чем система пока не может быть полезна и почему. Механизмы возврата
    # из vision предполагают архив, которого ещё нет: они не ломаются,
    # а тихо ничего не делают, и отличить это от поломки владелец не может.
    topics: int
    archive_age_days: int
    touches: Touches
    quota: Quota


async def _touches(now: datetime, timezone: str) -> Touches:
    """Что сегодня было с касаниями. Собирается отдельно — это про Фазу 4.

    Утренняя сводка спрашивается по **сегодняшнему** дню владельца, а не
    «последняя вообще»: вопрос, ради которого строка существует, звучит как
    «а сегодня-то она была?».
    """
    day = digest.slots.local_date(now, timezone)
    today = await digest.touches.of_day(day)
    budget = await digest.touches.budget(day)
    open_commitments, open_loops = await digest.service.counts(now)

    return Touches(
        morning=today.get(TouchSlot.MORNING),
        spent=budget.spent,
        budget=budget.limit,
        quiet_until=await digest.quiet.until(now),
        open_commitments=open_commitments,
        open_loops=open_loops,
        auto_resolved=await digest.evening.auto_resolved_count(),
    )


async def _quota(now: datetime, limits: tuple[int | None, int | None]) -> Quota:
    """Расход запросов за текущие сутки провайдера.

    Тиры считаются раздельно: квоты у них разные, и «осталось 40» про один
    ничего не говорит про другой.
    """
    embedding_limit, enrich_limit = limits
    return Quota(
        embedding=await quota_counter.used(now, tier=EMBEDDING, limit=embedding_limit),
        enrich=await quota_counter.used(now, tier=CHEAP, limit=enrich_limit),
        resets_at=quota_counter.resets_at(now),
    )


async def collect(
    timezone: str, *, quota_limits: tuple[int | None, int | None] = (None, None)
) -> SystemStatus:
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

        # Расшифровки, до которых чистка не дошла.
        #
        # Считаются **не все** пустые `clean_text`, и это не хитрость,
        # а следствие решений. Два отсева:
        #
        # 1. Отсчёт от первой вычищенной расшифровки. Старые записи задним
        #    числом не чистятся — квота и отрезки уже заведённых дел дороже
        #    единообразия (ADR-0015, пункт 3), — и, считая всё подряд, строка
        #    показывала бы вечные семьдесят с лишним. Ни разу не чистили —
        #    счётчик показывает весь архив, и это правда: чистка не работала
        #    никогда.
        # 2. Приватные записи. Их чистить нельзя вовсе (инвариант 5), поэтому
        #    их `clean_text` пуст навсегда — и это не отставание, а норма.
        #    Приватность спрашивается **сейчас** (`is_private_now`), а не
        #    по колонке: её включают задним числом уже после расшифровки.
        #
        # **Чем счётчик всё-таки врёт, вслух.** Расшифровка, на которой модель
        # вернула негодный ответ, остаётся с пустым `clean_text` навсегда
        # и считается отставанием вечно. Отличить «не дошли» от «дошли
        # и не получилось» сейчас нечем: следа о неудачной попытке в базе
        # не остаётся. Признак понадобится — заводить его надо строкой
        # о попытке, а не догадкой по времени.
        # Когда вычистили первую расшифровку. NULL — не чистили ни разу.
        cleaning_started = (
            select(func.min(Transcript.created_at))
            .where(Transcript.clean_text.is_not(None))
            .scalar_subquery()
        )
        without_cleanup = (
            await session.scalar(
                select(func.count())
                .select_from(Transcript)
                .join(Entry, Entry.id == Transcript.source_entry_id)
                .where(
                    Transcript.clean_text.is_(None),
                    # Приватность спрашивается тем же единственным выражением,
                    # что и везде: своё условие рядом однажды разъехалось бы
                    # с ним молча.
                    ~repository.is_private_now(),
                    or_(
                        cleaning_started.is_(None),
                        Transcript.created_at >= cleaning_started,
                    ),
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

        topics = await session.scalar(select(func.count()).select_from(Topic)) or 0
        # Возраст архива, а не число записей: «нужна неделя» — это про время,
        # и двадцать записей за один вечер недели не делают.
        oldest = await session.scalar(select(func.min(Entry.created_at)))

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
        transcripts_without_cleanup=int(without_cleanup),
        entries_without_index=len(await retrieval.entries_needing_index()),
        entries_without_annotation=await enrich.unannotated_count(),
        # Считается через вечерний разбор, а не по состоянию разметки: с Фазы 4
        # спорная запись уходит из очереди, когда с ней разобрались, — а состояние
        # в `annotations` при этом не меняется, потому что разметка это то, что
        # модель действительно сказала.
        waiting_for_review=await digest.evening.waiting_count(),
        trigrams_work=bool(trigrams),
        jobs_queued=int(queue.active),
        jobs_failed=int(queue.failed),
        next_reminder=(
            None if upcoming is None else NextReminder(fire_at=upcoming[0], text=upcoming[1])
        ),
        private_mode=await repository.is_private_mode(),
        topics=int(topics),
        archive_age_days=0 if oldest is None else (datetime.now(UTC) - oldest).days,
        touches=await _touches(datetime.now(UTC), timezone),
        quota=await _quota(datetime.now(UTC), quota_limits),
    )
