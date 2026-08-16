"""Сборка и запуск бота.

Polling, а не вебхуки: домена и TLS не требует, а Telegram хранит апдейты
около суток — перезапуск бота ничего не теряет (ADR-0001).

Два бота, dev и prod, различаются только токеном из окружения. Перепутать их
нельзя: у каждого свой `.env` и своя база. Смысл — отладка не портит архив.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from brain.adapters.telegram.handlers import build_router
from brain.adapters.telegram.whitelist import WhitelistMiddleware
from brain.config import Settings, get_runtime_settings
from brain.storage.db import dispose_engine

logger = logging.getLogger(__name__)


# Меню команд: то, что Telegram подсказывает при вводе «/». Список короткий
# намеренно — команд ровно столько, сколько их есть, и каждая описана так,
# чтобы не приходилось вспоминать синтаксис.
COMMANDS = [
    BotCommand(command="status", description="всё ли работает"),
    BotCommand(command="private", description="приватный режим: on / off / без аргумента"),
    BotCommand(command="start", description="что я умею"),
]


async def register_commands(bot: Bot) -> None:
    """Показать команды подсказкой при вводе.

    Ставится при каждом старте, а не один раз руками: список команд живёт
    в коде рядом с обработчиками, и это единственный способ не дать ему
    разъехаться с тем, что бот на самом деле понимает.
    """
    await bot.set_my_commands(COMMANDS)


def build_dispatcher(settings: Settings) -> Dispatcher:
    """Диспетчер с whitelist'ом впереди всего.

    Middleware вешается на внешний уровень: чужое сообщение не должно доходить
    ни до одного обработчика, включая команды.
    """
    dispatcher = Dispatcher()
    whitelist = WhitelistMiddleware(settings.telegram_allowed_user_ids)
    dispatcher.message.outer_middleware(whitelist)
    dispatcher.include_router(build_router(settings))
    return dispatcher


async def run() -> None:
    """Поднять бота. Падает на старте, если окружение задано неполно."""
    settings = get_runtime_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = build_dispatcher(settings)

    await register_commands(bot)

    logger.info("бот запущен: окружение %s", settings.brain_env)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
