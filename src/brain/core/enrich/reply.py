"""Что владелец видит, когда разметка готова.

Сообщение собирается **из данных**, а не дописывается к предыдущему тексту.
Причина простая: подтверждение приёма живёт в Telegram, а не у нас, и прочитать
его текст мы не можем — только заменить. Значит, всё, что там было, надо уметь
восстановить: расшифровку из `transcripts`, напоминание из `reminders`,
разметку из `annotations`. Так и сделано.

Русские названия типов живут здесь и только здесь. Это не та таблица перевода,
от которой отказался ADR-0011: там речь про соответствие «ответ модели →
значение колонки», и оно убрано намеренно. Здесь же — показ, и без него
владелец читал бы в боте `two_hypotheses`.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from brain.storage.models.annotation import AnnotationState, AnnotationType

NAMES: dict[AnnotationType, str] = {
    AnnotationType.THOUGHT: "мысль",
    AnnotationType.TASK: "задача",
    AnnotationType.EVENT: "событие",
    AnnotationType.LOOP: "открытый вопрос",
    AnnotationType.DECISION: "решение",
    AnnotationType.ERROR: "ошибка",
    AnnotationType.INSIGHT: "понимание",
    AnnotationType.REFUSAL: "отказ",
    AnnotationType.NOISE: "служебное",
}

STORED = "Записал."
UNCLEAR = "📌 не разобрал — разберу вечером"


@dataclass(frozen=True, slots=True)
class Shown:
    """Всё, что нужно показать про одну запись."""

    body: str
    type: AnnotationType
    alternative_type: AnnotationType | None
    state: AnnotationState
    topics: list[str]
    reminder_at: datetime | None = None
    reminder_text: str | None = None
    timezone: str = "UTC"


def render(shown: Shown) -> str:
    """Собрать сообщение целиком."""
    parts = [shown.body]

    if shown.reminder_at is not None and shown.reminder_text:
        due = shown.reminder_at.astimezone(ZoneInfo(shown.timezone))
        parts.append(f"⏰ {shown.reminder_text} — {due:%d.%m в %H:%M}")

    parts.append(_annotation_line(shown))
    return "\n\n".join(parts)


def _annotation_line(shown: Shown) -> str:
    """Строка разметки. Три состояния — три разных фразы, а не значок.

    Владелец должен видеть разницу между «уверен», «колеблюсь» и «не понял»
    словами: значок читается как украшение, а это единственное, ради чего
    состояние вообще заведено (ADR-0003).
    """
    if shown.state is AnnotationState.UNCLEAR:
        return UNCLEAR

    line = f"📌 {NAMES[shown.type]}"
    if shown.state is AnnotationState.TWO_HYPOTHESES and shown.alternative_type is not None:
        line += f" (или {NAMES[shown.alternative_type]} — уточню вечером)"
    if shown.topics:
        line += " · " + ", ".join(shown.topics)
    return line
