"""Как состояние системы выглядит в переписке.

Оформление живёт в адаптере, а не в ядре: ядро отдаёт факты, а решение
«что показать первым» — про телефон и про то, что с него читают на ходу.

Первым идёт воркер. Он единственное, что здесь может быть сломано незаметно:
бот отвечает сам за себя, и если пришёл ответ на `/status`, то бот жив
по построению.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from brain.core.quota import ModelUsage
from brain.core.status import SystemStatus, Touches
from brain.storage.models.touch import SilenceReason

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

    if status.transcripts_without_cleanup:
        # Отставание чистки. Показывается всегда, когда оно есть: одна-две
        # записи — это нормальный ход очереди, десятки означают, что чистка
        # придержана квотой или лежит, и в обоих случаях владелец видит
        # в сообщениях свои «э» и должен знать, почему.
        lines.append(f"🧹 Расшифровок без чистки: {status.transcripts_without_cleanup}")

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

    lines.extend(_quota(status, timezone=timezone))

    lines.append("")
    lines.extend(_touches(status, timezone=timezone))

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


# Почему сводка промолчала — словами. Это ровно то, ради чего в журнале
# касаний вообще есть строки с молчанием: без них «сводки не было» означало бы
# и «сказать нечего», и «воркер лежал», и различить их было бы нечем.
SILENCE = {
    SilenceReason.NOTHING_TO_SAY: "нечего было сказать",
    SilenceReason.QUIET: "вы просили молчать",
    SilenceReason.OVER_BUDGET: "бюджет касаний исчерпан",
    # Четвёртая причина — не решение промолчать, а несостоявшееся решение.
    # Формулировка поэтому другая по роду: три первых объясняют молчание,
    # эта сообщает о поломке.
    SilenceReason.FAILED: "механизм упал, пробую снова",
}


def _quota(status: SystemStatus, *, timezone: str) -> list[str]:
    """Сколько запросов к провайдеру осталось на сегодня — **по моделям**.

    Появилось после 20 августа 2026, когда воркер упёрся в суточную квоту
    эмбеддингов вслепую и не поднялся вовсе. Отвечает на единственный вопрос,
    которого тогда не хватило: сколько ещё можно.

    **Одним числом на всё это не отвечается**, и с 26 августа 2026 не
    отвечается вовсе: лимит провайдер держит на модель
    (`GenerateRequestsPerDayPerProjectPerModel-FreeTier` в теле `429`), а в один
    день система зовёт четыре разных. «Потрачено 58» складывало расход модели,
    у которой осталось ноль из двадцати, с расходом модели, у которой лимит
    другой и заведомо больше.

    Показывается **только то, о чём есть что сказать**, — как и остальные
    разделы `/status`. Молчит модель, у которой известный лимит истрачен меньше
    чем наполовину и отказов не было: строка, каждый день сообщающая «всё
    в порядке», перестаёт читаться ровно к тому дню, когда она нужна.
    """
    worth_saying = [usage for usage in status.quota.models if _worth_saying(usage)]
    if not worth_saying:
        return []

    resets = status.quota.resets_at.astimezone(ZoneInfo(timezone))
    return [
        f"📊 Запросов сегодня, сброс в {resets:%H:%M}",
        *(f"   {_model_quota(usage)}" for usage in worth_saying),
    ]


def _worth_saying(usage: ModelUsage) -> bool:
    """Есть ли про модель новость.

    Неизвестный лимит — всегда новость: остатка нет, и единственное, чем
    владелец может распорядиться, это само число потраченного.
    """
    if usage.refused:
        return True
    if usage.limit is None:
        return usage.spent > 0
    return usage.spent >= usage.limit // 2


def _model_quota(usage: ModelUsage) -> str:
    """Строка про одну модель. Лимит — только если он **известен**.

    Выдумывать его нельзя: число из головы читается как измеренное — та же
    ошибка, из-за которой в проекте цены помечены датой проверки.
    """
    if usage.limit is None:
        spent = f"{usage.model}: {usage.spent} — лимит неизвестен"
    else:
        spent = f"{usage.model}: {usage.spent} из {usage.limit}"

    if usage.refused:
        # Отказ провайдера — это его собственный ответ «больше нельзя»,
        # и он вернее нашего счётчика: тем же ключом мог пользоваться
        # кто-то ещё, и тогда наш счёт занижен.
        spent += f", отказов {usage.refused}"

    return f"🔴 {spent}" if usage.exhausted or usage.refused else spent


def _touches(status: SystemStatus, *, timezone: str) -> list[str]:
    """Что сегодня было с касаниями.

    Первой строкой — утренняя сводка, и у неё три разных состояния, а не два:
    пришла · промолчала и вот почему · **не дошла до решения вовсе**. Третье
    означает, что воркер не работал в нужный час, и отличается от второго
    отсутствием строки в журнале.
    """
    touches = status.touches
    lines = [_morning(touches, timezone=timezone)]

    if touches.quiet_until is not None:
        until = touches.quiet_until.astimezone(ZoneInfo(timezone))
        lines.append(f"🔇 Молчу до {until:%d.%m %H:%M} — снять: /resume")

    hanging = []
    if touches.open_commitments:
        hanging.append(f"дел {touches.open_commitments}")
    if touches.open_loops:
        hanging.append(f"открытых вопросов {touches.open_loops}")
    if hanging:
        lines.append(f"📌 Висит: {', '.join(hanging)}")

    if touches.habits:
        # Отдельной строкой, а не в «Висит»: привычка не висит и не ждёт
        # закрытия — она наступает снова (`vision.md` §5.11). Попав в тот же
        # список, она читалась бы как незакрытое дело, то есть ровно так,
        # как её читать нельзя.
        lines.append(f"🌱 Привычек: {touches.habits}")

    lines.append(f"💬 Касаний сегодня: {touches.spent} из {touches.budget}")

    if touches.failures:
        # Отказы — сразу под касаниями и до всего прочего: это единственное
        # место, где владелец узнаёт, что механизм сломался. Сама система
        # об этом не пишет — сообщение «у меня сломалось» требует внимания,
        # а сообщение обязано давать (`vision.md` §7.0, принцип 1).
        broken = ", ".join(
            name if count == 1 else f"{name} ×{count}" for name, count in touches.failures.items()
        )
        lines.append(f"⚠️ Сегодня упало в тике: {broken}")

    if touches.auto_resolved:
        # Владельцу об этом не сообщают уведомлением (`vision.md` §7.0,
        # принцип 2), но спросить он вправе — и здесь ответ.
        lines.append(f"🕗 Зафиксировано само по сроку: {touches.auto_resolved}")

    return lines


def _morning(touches: Touches, *, timezone: str) -> str:
    """Строка про утреннюю сводку — три состояния, а не два."""
    if touches.morning is None:
        return "☀️ Сводки сегодня ещё не было"

    if touches.morning.sent_at is not None:
        sent = touches.morning.sent_at.astimezone(ZoneInfo(timezone))
        return f"☀️ Сводка пришла в {sent:%H:%M}"

    reason = touches.morning.silence_reason
    explained = SILENCE.get(reason, "без объяснения") if reason else "без объяснения"
    return f"☀️ Сводка сегодня промолчала: {explained}"


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
