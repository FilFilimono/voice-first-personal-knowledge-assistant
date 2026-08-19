"""Как касания выглядят словами.

Текст живёт в ядре, а не в адаптере, — по образцу `core/enrich/reply.py`.
Граница проведена так: **что сказано** решает ядро, **чем это нажимается** —
адаптер. Ядро не знает, что кнопки бывают inline; адаптер не знает, почему
дела показываются старейшими.

Тон задан `vision.md` §7.0 и проверяется здесь построчно:

- **никаких счётчиков-достижений.** «Закрыл 4 дела» в ревью — это констатация
  за период, а не балл, и рядом с ней нет ни процента, ни серии;
- **никакого упрёка.** «Со вчера ждут разбора» — состояние на сейчас,
  а не «ты вчера не разобрал». Разница в том, к какому времени относится
  предложение, и она здесь единственная защита от тона, который прогоняет.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from brain.core.digest.evening import Disputed
from brain.core.digest.morning import Morning
from brain.core.digest.resurface import Old
from brain.core.digest.weekly import Weekly
from brain.core.enrich.reply import NAMES

# Сколько символов записи показывать в списке. Больше — и сводка перестаёт
# читаться за сорок секунд, меньше — и по обрывку не узнать, о чём речь.
SNIPPET = 70

# Возраст петли, после которого `vision.md` §6 велит читать его как перегрузку,
# а не как лень. Формулировка меняется вместе с числом — иначе цифра сама
# по себе прочитается как упрёк.
OVERLOADED_DAYS = 30


def snippet(text: str, limit: int = SNIPPET) -> str:
    """Кусок записи одной строкой."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def morning(content: Morning) -> str:
    """Утренняя сводка. Смотрит вперёд: «вот что сегодня висит»."""
    lines = ["☀️ Утро."]

    if content.commitments:
        lines += ["", "Висит:"]
        # Дела нумеруются, потому что закрываются кнопками, а текст дела
        # в подпись кнопки не помещается: на телефоне это два-три слова.
        # Номер — единственный способ связать кнопку с делом, не обрезая
        # его до неузнаваемости.
        lines += [
            f"{number}. {snippet(item.text)}"
            for number, item in enumerate(content.commitments, start=1)
        ]
        hidden = content.open_commitments - len(content.commitments)
        if hidden > 0:
            lines.append(f"…и ещё {hidden}")

    if content.loop is not None:
        lines += ["", f"🔁 Ты хотел разобраться: {snippet(content.loop.question)}", "Сдвинулось?"]

    if content.unsorted:
        # Настоящее время намеренно: «ждут разбора» — это состояние на сейчас,
        # а «ты вчера не разобрал» — упрёк за прошедшее (§7.0, принцип 2).
        lines += ["", f"🤔 Со вчера ждут разбора: {len(content.unsorted)}. Покажу вечером."]

    return "\n".join(lines)


def evening(items: list[Disputed]) -> str:
    """Вечерний разбор: спорные записи пачкой.

    Приходит только когда есть что показать, поэтому пустого варианта здесь нет
    вовсе — и это не упущение, а то самое «вечер приходит только с отдачей».
    """
    lines = ["🤔 Тут я не уверен. Подскажете?", ""]
    for number, item in enumerate(items, start=1):
        lines.append(f"{number}. {snippet(item.text)}")
        lines.append(f"   {_guess(item)}")
    return "\n".join(lines)


def _guess(item: Disputed) -> str:
    """Чем система считает запись и в чём именно сомневается."""
    if item.alternative_type is not None:
        return f"{NAMES[item.type]} или {NAMES[item.alternative_type]}?"
    return f"похоже на «{NAMES[item.type]}», но уверенности нет"


def resurfaced(old: Old, *, timezone: str) -> str:
    """Всплывшая запись. Без повода и без вопроса — на то и всплывание."""
    when = old.created_at.astimezone(ZoneInfo(timezone))
    return f"📄 Из архива, {when:%d.%m.%Y}:\n\n{snippet(old.text, limit=400)}"


def weekly(review: Weekly) -> str:
    """Недельное ревью — сведённые данные, а не рассказ.

    Рассказ придёт в Фазе 6 вместе с дорогим тиром; притворяться им сейчас
    значило бы выдать список за то, по чему `vision.md` §1.1 судит проект.
    """
    lines = ["🗓 Неделя.", ""]

    voiced = f", из них голосом {review.voice}" if review.voice else ""
    lines.append(f"Записей: {review.entries}{voiced}.")

    if review.topics:
        named = ", ".join(f"{name} ({count})" for name, count in review.topics[:5])
        lines.append(f"О чём: {named}.")

    if review.done:
        lines.append(f"Закрыл: {len(review.done)}.")
    if review.dropped:
        lines.append(f"Убрал как неактуальное: {len(review.dropped)}.")

    hanging = _hanging(review)
    if hanging:
        lines.append(hanging)

    if review.hint:
        lines += ["", f"💡 {review.hint}"]

    return "\n".join(lines)


def _hanging(review: Weekly) -> str:
    """Что осталось висеть — и как это читать, если висит давно."""
    parts = []
    if review.open_commitments:
        parts.append(f"дел {review.open_commitments}")
    if review.open_loops:
        parts.append(f"открытых вопросов {review.open_loops}")
    if not parts:
        return ""

    line = f"Висит: {', '.join(parts)}."
    if review.oldest_loop_days is not None and review.oldest_loop_days > OVERLOADED_DAYS:
        # Формулировка из `vision.md` §6: старая петля — признак перегрузки,
        # а не лени, и сказать это вслух дешевле, чем дать цифре прочитаться
        # самой.
        line += f" Старейший вопрос висит {review.oldest_loop_days} дней — это перегрузка."
    return line


def local(moment: datetime, timezone: str) -> datetime:
    """Момент в таймзоне владельца — для дат в тексте."""
    return moment.astimezone(ZoneInfo(timezone))
