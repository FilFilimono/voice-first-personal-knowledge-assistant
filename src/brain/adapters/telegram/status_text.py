"""Как состояние системы выглядит в переписке.

Оформление живёт в адаптере, а не в ядре: ядро отдаёт факты, а решение
«что показать первым» — про телефон и про то, что с него читают на ходу.

Первым идёт воркер. Он единственное, что здесь может быть сломано незаметно:
бот отвечает сам за себя, и если пришёл ответ на `/status`, то бот жив
по построению.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from brain.core.status import SystemStatus

ALIVE = "✅ Воркер жив"
DEAD = "🔴 Воркер молчит"
NEVER = "🔴 Воркер ни разу не отмечался"
START_WORKER = "Запустите: just worker"

# Локаль базы `C` — и pg_trgm не видит в кириллице букв. Поиск при этом
# работает двумя каналами из трёх и выглядит исправным.
TRIGRAMS_DEAD = (
    "⚠️ Триграммы не работают на кириллице — база создана с локалью C. "
    "Поиск идёт двумя каналами из трёх (см. docs/status.md)"
)


def render(status: SystemStatus, *, timezone: str) -> str:
    """Собрать сообщение для `/status`."""
    lines = [_worker(status), ""]

    lines.append(f"📥 Записей: {status.entries}")
    if status.voice_without_transcript:
        lines.append(f"⏳ Голосовых без расшифровки: {status.voice_without_transcript}")

    if status.entries_without_index:
        lines.append(f"🔍 Не в поиске: {status.entries_without_index} — поставить: just reindex")

    if status.entries_without_annotation:
        lines.append(
            f"🏷 Без разметки: {status.entries_without_annotation} — поставить: just reenrich"
        )

    if status.waiting_for_review:
        # Это не поломка, а нормальная работа: модель колеблется или не поняла.
        # Показывается затем, что вечерний разбор придёт в Фазе 4, и до тех пор
        # очередь на него растёт молча.
        lines.append(f"🤔 Ждут разбора: {status.waiting_for_review}")

    if not status.trigrams_work:
        # Третий канал поиска молчит, а первые два работают: выдача становится
        # хуже, но не пустой, и заметить это без строки здесь невозможно.
        lines.append(TRIGRAMS_DEAD)

    lines.append(f"⚙️ Заданий в очереди: {status.jobs_queued}")
    if status.jobs_failed:
        # Упавшие не исчезают, а ждут решения — поэтому им место в отчёте
        # вместе с командой, которая их возвращает.
        lines.append(f"❌ Упало: {status.jobs_failed} — вернуть: just failed, just requeue <id>")

    lines.append("")
    lines.append(_reminder(status, timezone=timezone))
    lines.append(
        "🔒 Приватный режим включён — голосовые не принимаю"
        if status.private_mode
        else "🔓 Приватный режим выключен"
    )

    warming = _warming_up(status)
    if warming:
        lines.append("")
        lines.extend(warming)

    return "\n".join(lines)


# Сколько дней архива нужно механизму, чтобы он начал что-то делать.
# Числа не из головы: неделя — потому что недельное ревью пересказывает неделю,
# две — порог всплывания старого из vision (§5.3).
WEEK = 7
RESURFACING_AGE = 14

# Ниже этого числа тем список ещё не устоялся: на живом архиве из 37 записей
# разметка находила 6–7 тем, и половина из них — по одному разу.
FEW_TOPICS = 5


def _warming_up(status: SystemStatus) -> list[str]:
    """Почему система пока работает не в полную силу — словами, а не цифрами.

    Механизмы возврата предполагают архив, которого ещё нет. Они не ломаются,
    а тихо ничего не делают, и отличить «рано» от «сломалось» по поведению
    нельзя — снаружи и то и другое выглядит как молчание.

    **Про связи здесь не написано ничего, и это намеренно.** Замер 19.08.2026
    показал: беда с ними обратная ожидаемой — порог из ADR-0006 пропускает 37%
    всех пар записей, то есть связи не промолчат, а заговорят слишком часто.
    Пока инструмент взамен порога не выбран, писать про них хоть что-нибудь
    значит соврать владельцу в третий раз подряд.
    """
    days = status.archive_age_days
    waiting = []

    if days < WEEK:
        waiting.append(f"• пересказ недели — нужна неделя записей, пока {_days(days)}")
    if days < RESURFACING_AGE:
        waiting.append(f"• напоминания о старом — доставать пока нечего, архиву {_days(days)}")
    if status.topics < FEW_TOPICS:
        waiting.append(
            f"• разбор по темам — тем всего {status.topics}, "
            "они появляются по мере того, как вы пишете"
        )

    if not waiting:
        return []

    return [
        "📊 Система пока разгоняется",
        f"Записей: {status.entries}, тем: {status.topics}, архиву {_days(days)}.",
        "Уже работает: приём голоса и текста, напоминания, поиск по архиву, разбор записей.",
        "Ждёт наполнения:",
        *waiting,
    ]


def _days(days: int) -> str:
    """«4 дня», «1 день», «11 дней» — без библиотек."""
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дня"
    return f"{days} дней"


def _worker(status: SystemStatus) -> str:
    """Строка про воркер — со временем, а не только с цветом.

    «Молчит» без времени бесполезно: разница между «минуту назад перезапустили»
    и «лежит со вчера» — это разница между «подождать» и «чинить».
    """
    if status.worker.silent_for is None:
        return f"{NEVER}. {START_WORKER}"

    if status.worker.alive:
        return ALIVE

    return f"{DEAD} уже {_ago(status.worker.silent_for)}. {START_WORKER}"


def _reminder(status: SystemStatus, *, timezone: str) -> str:
    if status.next_reminder is None:
        return "⏰ Напоминаний нет"

    local = status.next_reminder.fire_at.astimezone(ZoneInfo(timezone))
    return f"⏰ Ближайшее: {local:%d.%m в %H:%M} — {status.next_reminder.text}"


def _ago(silent: timedelta) -> str:
    """«12 мин», «3 ч», «2 дн» — без секунд и без библиотек."""
    minutes = int(silent.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"

    return f"{hours // 24} дн"
