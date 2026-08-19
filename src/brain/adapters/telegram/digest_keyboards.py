"""Кнопки под касаниями Фазы 4.

Отдельным файлом от `notifier.py`, потому что кнопок стало много и они про
разное: там — приём записи, здесь — сводка, дела, петли и вечерний разбор.

**Состояния кнопок нигде не хранится.** Всё, что нужно обработчику, уезжает
в `callback_data` и возвращается с нажатием — как у вопроса про напоминание.
Поэтому кнопки переживают перезапуск бота, а `touches.payload` служит только
для восстановления списка, когда идентификаторов больше, чем влезает в 64
байта.

**Подписи короткие намеренно.** На телефоне в ряд помещается два-три слова,
а текст дела в подпись не влезает вовсе — поэтому дела в сводке пронумерованы,
и кнопка называет номер, а не содержание.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from brain.storage.models.annotation import AnnotationType

# Дела: `deed:done:17`, `deed:drop:17`.
DEED_PREFIX = "deed"
DONE = "done"
DROP = "drop"

# Петли: `loop:closed:3`. `new` — заведение петли по кнопке под разбором.
LOOP_PREFIX = "loop"
CLOSED = "closed"
THINKING = "think"
IRRELEVANT = "gone"
REPHRASE = "again"
NEW = "new"

# Вечерний разбор: `even:ok:17`, `even:wrong:17`, `even:set:task:17`.
EVENING_PREFIX = "even"
OK = "ok"
WRONG = "wrong"
SET = "set"


def deed_data(action: str, commitment_id: int) -> str:
    return f"{DEED_PREFIX}:{action}:{commitment_id}"


def loop_data(action: str, loop_id: int) -> str:
    return f"{LOOP_PREFIX}:{action}:{loop_id}"


def evening_data(action: str, entry_id: int) -> str:
    return f"{EVENING_PREFIX}:{action}:{entry_id}"


def morning_keyboard(
    commitment_ids: list[int], loop_id: int | None, *, loop_stuck: bool
) -> InlineKeyboardMarkup | None:
    """Кнопки под утренней сводкой: по ряду на дело плюс ряд петли.

    `None`, если нажимать нечего: сводка из одного «со вчера ждут разбора»
    кнопок не требует, а пустая клавиатура занимает место на экране.
    """
    rows: list[list[InlineKeyboardButton]] = []

    for number, commitment_id in enumerate(commitment_ids, start=1):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{number} ✓ сделал", callback_data=deed_data(DONE, commitment_id)
                ),
                InlineKeyboardButton(
                    text=f"{number} ✕ неактуально", callback_data=deed_data(DROP, commitment_id)
                ),
            ]
        )

    if loop_id is not None:
        rows.append(_loop_row(loop_id, stuck=loop_stuck))

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _loop_row(loop_id: int, *, stuck: bool) -> list[InlineKeyboardButton]:
    """Кнопки под петлёй. После третьего откладывания вопрос другой.

    `vision.md` §5.2: петля перестаёт быть раздражителем. «Ещё думаю» здесь
    не предлагается вовсе — в этом весь смысл смены вопроса.
    """
    if stuck:
        return [
            InlineKeyboardButton(text="Закрыть", callback_data=loop_data(CLOSED, loop_id)),
            InlineKeyboardButton(text="Переформулирую", callback_data=loop_data(REPHRASE, loop_id)),
        ]
    return [
        InlineKeyboardButton(text="Закрыл", callback_data=loop_data(CLOSED, loop_id)),
        InlineKeyboardButton(text="Ещё думаю", callback_data=loop_data(THINKING, loop_id)),
        InlineKeyboardButton(text="Неактуально", callback_data=loop_data(IRRELEVANT, loop_id)),
    ]


def evening_keyboard(entry_ids: list[int]) -> InlineKeyboardMarkup:
    """Кнопки под вечерним разбором: по ряду на спорную запись."""
    rows = [
        [
            InlineKeyboardButton(text=f"{number} ок", callback_data=evening_data(OK, entry_id)),
            InlineKeyboardButton(
                text=f"{number} не туда", callback_data=evening_data(WRONG, entry_id)
            ),
        ]
        for number, entry_id in enumerate(entry_ids, start=1)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def evening_types_keyboard(entry_id: int, names: dict[AnnotationType, str]) -> InlineKeyboardMarkup:
    """Список типов — то, что открывается после «не туда» в вечернем разборе.

    Свой, а не общий с разбором записи: там нажатие заменяет весь текст
    сообщения на «📌 тип», а здесь текст — пачка из нескольких записей,
    и стирать её из-за одной поправки нельзя.
    """
    buttons = [
        InlineKeyboardButton(
            text=names[member],
            callback_data=f"{EVENING_PREFIX}:{SET}:{member.value}:{entry_id}",
        )
        for member in AnnotationType
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)
