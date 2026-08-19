"""`Notifier` поверх aiogram.

Через него воркер редактирует подтверждение приёма, ничего не зная о Telegram.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from brain.storage.models.annotation import AnnotationType

logger = logging.getLogger(__name__)


# Приставка в `callback_data`, по которой обработчик узнаёт своё нажатие.
# Формат: `reminder:yes:17`. Telegram отводит на всё 64 байта — с запасом.
CALLBACK_PREFIX = "reminder"
YES = "yes"
NO = "no"

# Кнопки под готовой разметкой. Приставка своя, чтобы нажатия не путались
# с ответом на «это напоминание?»: сообщение одно и то же, а смысл разный.
ANNOTATION_PREFIX = "mark"
WRONG = "wrong"
PRIVATE = "private"
# `mark:set:task:17` — выбранный тип в списке, который открывает «не туда».
SET = "set"


def answer_data(answer: str, entry_id: int) -> str:
    """Что вернётся в бот при нажатии кнопки."""
    return f"{CALLBACK_PREFIX}:{answer}:{entry_id}"


def yes_no_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    """Кнопки «Да» и «Нет» под вопросом «это напоминание?».

    Живёт здесь, а не в обработчике, потому что вопрос задают двое: бот
    при приёме текста и воркер после расшифровки. Разъехавшиеся `callback_data`
    ловились бы только в проде.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=answer_data(YES, entry_id)),
                InlineKeyboardButton(text="Нет", callback_data=answer_data(NO, entry_id)),
            ]
        ]
    )


def annotation_data(action: str, entry_id: int) -> str:
    """Что вернётся в бот при нажатии кнопки под разметкой."""
    return f"{ANNOTATION_PREFIX}:{action}:{entry_id}"


def annotation_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    """Две кнопки под готовым разбором.

    «Не туда» открывает список типов, а не правит что-то само: нажатие без
    указания верного типа сказало бы «модель ошиблась» и ничего не дало бы
    ни архиву, ни eval'у. Два нажатия вместо одного — цена за то, чтобы
    поправка была материалом, а не жалобой.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Не туда", callback_data=annotation_data(WRONG, entry_id)
                ),
                InlineKeyboardButton(
                    text="🔒 Приватная", callback_data=annotation_data(PRIVATE, entry_id)
                ),
            ]
        ]
    )


def type_keyboard(entry_id: int, names: dict[AnnotationType, str]) -> InlineKeyboardMarkup:
    """Список типов — то, что открывается после «не туда».

    По две кнопки в ряд: девять названий в один столбец занимают пол-экрана
    телефона, а выбирать приходится глядя на текст записи выше.
    """
    buttons = [
        InlineKeyboardButton(
            text=names[member],
            callback_data=f"{ANNOTATION_PREFIX}:{SET}:{member.value}:{entry_id}",
        )
        for member in AnnotationType
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramNotifier:
    """Отправка и правка сообщений."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> int:
        message = await self._bot.send_message(chat_id, text)
        return message.message_id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Заменить текст подтверждения на готовый результат.

        Telegram отвечает ошибкой, если новый текст совпадает со старым или
        сообщение уже удалено. Ни то ни другое не повод считать задание
        упавшим и повторять расшифровку — она уже сохранена.

        Разметка при этом снимается: правка без `reply_markup` убирает кнопки,
        и отвеченный вопрос перестаёт выглядеть ждущим ответа.
        """
        await self._edit(chat_id, message_id, text, markup=None)

    async def ask_yes_no(self, chat_id: int, message_id: int, text: str, *, entry_id: int) -> None:
        """Превратить подтверждение приёма в вопрос с кнопками «Да» и «Нет»."""
        await self._edit(chat_id, message_id, text, markup=yes_no_keyboard(entry_id))

    async def show_annotation(
        self, chat_id: int, message_id: int, text: str, *, entry_id: int
    ) -> None:
        """Дописать разметку к подтверждению приёма и повесить две кнопки.

        Это по-прежнему то же самое сообщение: одна запись стоит одного
        уведомления, и разбор — не повод его удваивать. Правка сообщения
        в Telegram уведомления не шлёт, так что появление разметки владельца
        не дёргает.
        """
        await self._edit(chat_id, message_id, text, markup=annotation_keyboard(entry_id))

    async def _edit(
        self, chat_id: int, message_id: int, text: str, *, markup: InlineKeyboardMarkup | None
    ) -> None:
        try:
            await self._bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except TelegramBadRequest as error:
            logger.warning("не удалось отредактировать сообщение %s: %s", message_id, error)
