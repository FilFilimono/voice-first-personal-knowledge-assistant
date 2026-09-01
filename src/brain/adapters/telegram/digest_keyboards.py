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

**У дела три уровня, а не один экран.** Четыре действия на каждое из трёх дел
— это двенадцать кнопок под сообщением, которое обязано читаться за сорок
секунд:

    1  2  3                                     свёрнуто, по кнопке на дело
      ↓ 2
    Завершить · Взять в работу · Отложить · Убрать        Назад
      ↓ Отложить
    До завтра · На неделю · На месяц                      Назад
      ↓ Завершить, Отложить или Убрать
    Отложено · Вернуть                          отмена вместо «вы уверены?»

Разворачивается ряд **одного** дела; строки остальных не меняются. «Назад»
обязательна на каждом уровне: без неё промах по кнопке — тупик.

**«Взять в работу» из этой лестницы выпадает намеренно**: после него ряд
остаётся развёрнутым, и кнопки возврата у него нет. Три остальных перехода
убирают дело из сводки — действовать после них не над чем. `in_progress`
оставляет дело на месте, и свёрнутый в подтверждение ряд означал бы, что
помеченное «в работе» нельзя завершить до завтрашней сводки. Подробности —
`handlers._rows_after`.

Подписи утверждены владельцем и меняются только им. Правило, по которому они
выбраны, стоит помнить при любой правке: подпись говорит, **что произойдёт**,
а не спрашивает и не отвечает за владельца. Прежние «✓ сделал» и «✕
неактуально» — ответы на невидимый вопрос, в прошедшем времени и от первого
лица; у петли по той же причине свои глаголы — см. `_loop_row`.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from brain.storage.models.annotation import AnnotationType
from brain.storage.models.commitment import CommitmentState
from brain.storage.models.habit import Rhythm

# Дела: `deed:done:17`, `deed:until:week:17`, `deed:undo:in_progress:17`.
DEED_PREFIX = "deed"
PICK = "pick"
COLLAPSE = "back"
DONE = "done"
WORK = "work"
SNOOZE = "snooze"
DROP = "drop"
UNTIL = "until"
UNDO = "undo"
SEEN = "seen"

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

# Привычки: `habit:new:17` — заведение по кнопке под разбором,
# `habit:daily:17` — выбранный ритм, `habit:other:17` — второй ряд ритмов.
# Число всегда номер **записи**, а не привычки: привычки в этот момент ещё
# нет, а кнопка живёт в переписке вечно и должна работать и через месяц.
HABIT_PREFIX = "habit"
OTHER = "other"


def deed_data(action: str, commitment_id: int) -> str:
    return f"{DEED_PREFIX}:{action}:{commitment_id}"


def loop_data(action: str, loop_id: int) -> str:
    return f"{LOOP_PREFIX}:{action}:{loop_id}"


def evening_data(action: str, entry_id: int) -> str:
    return f"{EVENING_PREFIX}:{action}:{entry_id}"


def habit_data(action: str, entry_id: int) -> str:
    return f"{HABIT_PREFIX}:{action}:{entry_id}"


# Ритмы кнопками — ровно те, что названы в `vision.md` §5.11: каждый день ·
# через день · только по будням · свой вариант. Первые три в ряд, четвёртый
# разворачивает второй ряд.
#
# **Умолчания в этом списке нет намеренно.** Ни одна кнопка не подсвечена
# и ни одна не срабатывает сама: угаданный ритм — это ежедневное сообщение,
# выбранное за владельца, и ошибка в нём обнаружится не разговором,
# а раздражением (§5.11).
#
# «Свой вариант» **не открывает поле ввода**, а разворачивает второй ряд, и это
# то же решение, что у «Изменить формулировку» у петли: в этой системе способ
# что-то сказать — записать это, а строка, введённая в ответ на кнопку,
# не получила бы ни разметки, ни разбора и осела бы в чужой таблице. Ритмы
# второго ряда покрывают всё, что система умеет разбирать, кроме названных
# дней недели («по понедельникам и четвергам») — те приходят только из слов
# записи, потому что кнопкой их пришлось бы собирать из семи нажатий.
RHYTHM_LABELS: tuple[tuple[Rhythm, str], ...] = (
    (Rhythm.DAILY, "Каждый день"),
    (Rhythm.EVERY_OTHER_DAY, "Через день"),
    (Rhythm.WEEKDAYS, "По будням"),
)
OTHER_RHYTHM_LABELS: tuple[tuple[Rhythm, str], ...] = (
    (Rhythm.WEEKENDS, "По выходным"),
    (Rhythm.TIMES_PER_WEEK, "Раз в неделю"),
)
OTHER_LABEL = "Свой вариант"


def rhythm_keyboard(entry_id: int, *, other: bool = False) -> InlineKeyboardMarkup:
    """Чем спрашивают ритм, когда в записи его нет.

    Спрашивают, а не угадывают, — `vision.md` §5.11. Вопрос задаётся правкой
    того же сообщения, под которым нажали «Привычка»: одна запись стоит одного
    уведомления, и подтверждение ритма — часть того же разбора, а не второе
    сообщение.
    """
    labels = OTHER_RHYTHM_LABELS if other else RHYTHM_LABELS
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=habit_data(rhythm.value, entry_id))
            for rhythm, label in labels
        ]
    ]
    if not other:
        rows.append(
            [InlineKeyboardButton(text=OTHER_LABEL, callback_data=habit_data(OTHER, entry_id))]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Подписи утверждены владельцем и меняются только им. Правило, по которому
# они выбраны: подпись говорит, **что произойдёт**, а не спрашивает и не
# отвечает за владельца. Прежние «✓ сделал» и «✕ неактуально» — ответы
# на невидимый вопрос, в прошедшем времени и от первого лица.
CHOICE_LABELS = (
    (DONE, "Завершить"),
    (WORK, "Взять в работу"),
    (SNOOZE, "Отложить"),
    (DROP, "Убрать"),
)
BACK_LABEL = "Назад"
RETURN_LABEL = "Вернуть"

# Сроки. Именно «До завтра», а не «Завтра»: это дополнение к глаголу
# «Отложить», и читается как продолжение фразы.
TERM_LABELS = (("day", "До завтра"), ("week", "На неделю"), ("month", "На месяц"))

# Чем стала строка дела после нажатия. Безлично: «Завершено», а не «Завершил» —
# отчитываться за владельца система не должна.
STATE_LABELS = {
    CommitmentState.DONE: "Завершено",
    CommitmentState.IN_PROGRESS: "В работе",
    CommitmentState.SNOOZED: "Отложено",
    CommitmentState.DROPPED: "Убрано",
}


def morning_keyboard(
    commitment_ids: list[int], loop_id: int | None, *, loop_stuck: bool
) -> InlineKeyboardMarkup | None:
    """Кнопки под утренней сводкой: по ряду на дело плюс ряд петли.

    **По одной кнопке на дело, выбор не развёрнут.** Четыре действия на каждое
    из трёх дел — это двенадцать кнопок под одним сообщением, и сводка,
    читающаяся за сорок секунд, превращается в панель управления. Нажатие
    номера разворачивает выбор для этого дела и только для него.

    `None`, если нажимать нечего: пустая клавиатура занимает место на экране.
    """
    rows = [
        deed_row(number, commitment_id)
        for number, commitment_id in enumerate(commitment_ids, start=1)
    ]

    if loop_id is not None:
        rows.append(_loop_row(loop_id, stuck=loop_stuck))

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def deed_row(number: int, commitment_id: int) -> list[InlineKeyboardButton]:
    """Свёрнутая строка дела: только номер.

    Номер, а не текст: подпись кнопки на телефоне вмещает два-три слова,
    и дело в неё не влезает вовсе. Поэтому дела в сводке пронумерованы,
    и кнопка называет номер, а не содержание.
    """
    return [InlineKeyboardButton(text=str(number), callback_data=deed_data(PICK, commitment_id))]


def choice_rows(commitment_id: int) -> list[list[InlineKeyboardButton]]:
    """Второй уровень: что произойдёт с делом.

    «Назад» отдельным рядом и обязательна: без неё промах по кнопке — тупик,
    развернувшийся выбор нечем свернуть.
    """
    return [
        [
            InlineKeyboardButton(text=label, callback_data=deed_data(action, commitment_id))
            for action, label in CHOICE_LABELS
        ],
        [InlineKeyboardButton(text=BACK_LABEL, callback_data=deed_data(COLLAPSE, commitment_id))],
    ]


def term_rows(commitment_id: int) -> list[list[InlineKeyboardButton]]:
    """Третий уровень: на сколько отложить.

    «Назад» ведёт на второй уровень, а не на первый: сюда попадают, уже выбрав
    «Отложить», и передумать хочется обычно про срок, а не про всё сразу.
    """
    return [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"{DEED_PREFIX}:{UNTIL}:{term}:{commitment_id}"
            )
            for term, label in TERM_LABELS
        ],
        [InlineKeyboardButton(text=BACK_LABEL, callback_data=deed_data(PICK, commitment_id))],
    ]


def confirmed_row(
    commitment_id: int, state: CommitmentState, *, back_to: CommitmentState
) -> list[InlineKeyboardButton]:
    """Строка дела после нажатия: что стало и как вернуть.

    Только для трёх состояний, которые убирают дело из сводки. У `in_progress`
    подтверждения нет — см. шапку модуля.

    Отмена вместо вопроса «вы уверены?»: подтверждение раздражает на каждом
    нажатии, а промах случается редко — и отмена снимает его цену целиком.

    Прежнее состояние едет в `callback_data`, потому что по данным его уже
    не восстановить: дело могло быть «в работе» до того, как его завершили.
    Живёт это ровно столько, сколько живёт клавиатура сообщения, — то есть
    до следующей сводки, и протухать тут нечему.

    Первая кнопка нажатий не ждёт: подпись состояния — это надпись, а рядов
    без кнопок в Telegram не бывает.
    """
    return [
        InlineKeyboardButton(
            text=STATE_LABELS[state], callback_data=deed_data(SEEN, commitment_id)
        ),
        InlineKeyboardButton(
            text=RETURN_LABEL,
            callback_data=f"{DEED_PREFIX}:{UNDO}:{back_to.value}:{commitment_id}",
        ),
    ]


def deed_of(button: InlineKeyboardButton) -> int | None:
    """Дело, к которому относится кнопка. `None` — кнопка не про дела.

    Разбор идёт по последнему полю: у `deed:done:17` и `deed:until:week:17`
    оно одно и то же, а число полей разное.
    """
    data = button.callback_data or ""
    if not data.startswith(f"{DEED_PREFIX}:"):
        return None
    tail = data.rsplit(":", maxsplit=1)[-1]
    return int(tail) if tail.isdigit() else None


def deed_numbers(markup: InlineKeyboardMarkup) -> dict[int, int]:
    """Какой номер у какого дела — по клавиатуре, а не по памяти.

    Номера в тексте сводки и на кнопках обязаны совпадать, а текст мы после
    отправки не перечитываем. Порядок рядов сохраняется при любом
    разворачивании, поэтому номер восстанавливается из него: первое дело
    сверху — первое в списке.
    """
    order: list[int] = []
    for row in markup.inline_keyboard:
        for button in row:
            found = deed_of(button)
            if found is not None and found not in order:
                order.append(found)
    return {commitment_id: number for number, commitment_id in enumerate(order, start=1)}


def redraw(
    markup: InlineKeyboardMarkup, commitment_id: int, rows: list[list[InlineKeyboardButton]]
) -> InlineKeyboardMarkup:
    """Заменить ряды одного дела, не трогая остальные.

    Строки других дел не меняются — ни порядком, ни видом: владелец нажал
    на одно, и всё остальное на экране обязано остаться на месте. Поэтому
    чужие ряды переносятся как есть, а не собираются заново.
    """
    redrawn: list[list[InlineKeyboardButton]] = []
    replaced = False
    for row in markup.inline_keyboard:
        if any(deed_of(button) == commitment_id for button in row):
            if not replaced:
                redrawn.extend(rows)
                replaced = True
            continue
        redrawn.append(list(row))
    return InlineKeyboardMarkup(inline_keyboard=redrawn)


def _loop_row(loop_id: int, *, stuck: bool) -> list[InlineKeyboardButton]:
    """Кнопки под петлёй. После третьего откладывания вопрос другой.

    `vision.md` §5.2: петля перестаёт быть раздражителем. «Отложить» здесь
    не предлагается вовсе — в этом весь смысл смены вопроса.

    **Глаголы у петли свои, и это не вкусовщина.** У дела «Завершить»
    и «Убрать» различаются очевидно: сделал или отказался. У мысли, которую
    человек крутит в голове, «Закрыть» и «Убрать» звучат одинаково, и кнопку
    жали бы наугад, портя данные, по которым потом судят о петлях. «Решено»
    и «Отпало» несут разницу сами: решено — додумал, отпало — само ушло,
    думать не о чем. Форма безличная, поэтому это не отчёт за владельца,
    в отличие от убранного «сделал».
    """
    if stuck:
        return [
            InlineKeyboardButton(text="Решено", callback_data=loop_data(CLOSED, loop_id)),
            InlineKeyboardButton(
                text="Изменить формулировку", callback_data=loop_data(REPHRASE, loop_id)
            ),
            InlineKeyboardButton(text="Отпало", callback_data=loop_data(IRRELEVANT, loop_id)),
        ]
    return [
        InlineKeyboardButton(text="Решено", callback_data=loop_data(CLOSED, loop_id)),
        InlineKeyboardButton(text="Отложить", callback_data=loop_data(THINKING, loop_id)),
        InlineKeyboardButton(text="Отпало", callback_data=loop_data(IRRELEVANT, loop_id)),
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
