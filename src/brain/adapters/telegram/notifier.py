"""`Notifier` поверх aiogram.

Через него воркер редактирует подтверждение приёма, ничего не зная о Telegram.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from brain.adapters.telegram import digest_keyboards
from brain.core.digest import habits
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


def annotation_keyboard(
    entry_id: int, *, offer_loop: bool = False, offer_habit: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки под готовым разбором.

    «Не туда» открывает список типов, а не правит что-то само: нажатие без
    указания верного типа сказало бы «модель ошиблась» и ничего не дало бы
    ни архиву, ни eval'у. Два нажатия вместо одного — цена за то, чтобы
    поправка была материалом, а не жалобой.

    Третья и четвёртая кнопки появляются, когда разметка нашла открытый вопрос
    или привычку. Обе вещи заводятся **только с подтверждения** (`vision.md`
    §5.2 и §5.11), но спрашивать отдельным сообщением нельзя: одна запись стоит
    одного уведомления. У привычки цена ошибки при этом выше, чем у петли:
    молча заведённая привычка — это ежедневное сообщение, о котором не просили.
    """
    rows = [
        [
            InlineKeyboardButton(text="Не туда", callback_data=annotation_data(WRONG, entry_id)),
            InlineKeyboardButton(
                text="🔒 Приватная", callback_data=annotation_data(PRIVATE, entry_id)
            ),
        ]
    ]
    if offer_loop:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 Открытый вопрос",
                    callback_data=digest_keyboards.loop_data(digest_keyboards.NEW, entry_id),
                )
            ]
        )
    if offer_habit:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌱 Привычка",
                    callback_data=digest_keyboards.habit_data(digest_keyboards.NEW, entry_id),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        entry_id: int,
        offer_loop: bool = False,
    ) -> None:
        """Дописать разметку к подтверждению приёма и повесить кнопки.

        Это по-прежнему то же самое сообщение: одна запись стоит одного
        уведомления, и разбор — не повод его удваивать. Правка сообщения
        в Telegram уведомления не шлёт, так что появление разметки владельца
        не дёргает.

        **Про привычку спрашивает ядро, а не вызывающий код** — в отличие
        от петли, чей признак приезжает готовым. Причина в том, что признаков
        два, а не один: разметка нашла привычку **и** по этой записи её ещё
        не завели. Второе знает только `digest.habits`, и посчитанное здесь
        оно разошлось бы с тем, что делает нажатие: кнопка живёт в переписке
        вечно, а нажатая второй раз обязана не завести второе ежедневное
        сообщение. Инвариант 1 цел — адаптер идёт в базу через сервис ядра,
        а не сам.
        """
        await self._edit(
            chat_id,
            message_id,
            text,
            markup=annotation_keyboard(
                entry_id,
                offer_loop=offer_loop,
                offer_habit=await habits.proposal(entry_id) is not None,
            ),
        )

    async def send_morning(
        self,
        chat_id: int,
        text: str,
        *,
        commitment_ids: list[int],
        loop_id: int | None,
        loop_stuck: bool = False,
    ) -> int:
        """Утренняя сводка. Со звуком: это единственный якорь дня.

        Утренняя сводка — отдельная категория, не инициативная (`vision.md` §7),
        и правило «инициативные беззвучно» на неё не распространяется. Сводка,
        пришедшая беззвучно, не была бы якорем.
        """
        message = await self._bot.send_message(
            chat_id,
            text,
            reply_markup=digest_keyboards.morning_keyboard(
                commitment_ids, loop_id, loop_stuck=loop_stuck
            ),
        )
        return message.message_id

    async def send_evening(self, chat_id: int, text: str, *, entry_ids: list[int]) -> int:
        """Вечерний разбор. Беззвучно: это инициативное касание."""
        message = await self._bot.send_message(
            chat_id,
            text,
            reply_markup=digest_keyboards.evening_keyboard(entry_ids),
            disable_notification=True,
        )
        return message.message_id

    async def send_quietly(self, chat_id: int, text: str) -> int:
        """Беззвучно и без кнопок: всплывшая запись, недельное ревью."""
        message = await self._bot.send_message(chat_id, text, disable_notification=True)
        return message.message_id

    async def _edit(
        self, chat_id: int, message_id: int, text: str, *, markup: InlineKeyboardMarkup | None
    ) -> None:
        try:
            await self._bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except TelegramBadRequest as error:
            logger.warning("не удалось отредактировать сообщение %s: %s", message_id, error)
