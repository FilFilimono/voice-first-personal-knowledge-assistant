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

    return "\n".join(lines)


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
