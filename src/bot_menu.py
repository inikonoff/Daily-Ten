"""
Меню бота в Telegram: список команд («Menu» слева от поля ввода) и описание
бота. Регистрируется при каждом старте, чтобы совпадать с кодом. Ошибки не
валят запуск: меню удобство, а не необходимость.
"""

import logging
from typing import Iterable

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands

from src.config import PRODUCT_NAME

logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="ten", description="Start today's ten"),
    BotCommand(command="summary", description="My last Session Summary"),
    BotCommand(command="finish", description="Wrap up the session"),
    BotCommand(command="start", description="Where are we? (status)"),
]
ADMIN_COMMANDS = USER_COMMANDS + [BotCommand(command="admin", description="Admin panel")]

SHORT_DESCRIPTION = (
    f"{PRODUCT_NAME}: a daily 10-exchange English conversation and a personal summary of your English."
)
DESCRIPTION = (
    f"{PRODUCT_NAME} is a daily English conversation with Mrs. Smith: ten exchanges, "
    "then a personal Session Summary with your best moment, mistakes to fix and what "
    "to work on next. Write or send voice messages. Free."
)


async def setup_bot_menu(bot: Bot, admin_ids: Iterable[int]) -> None:
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await bot.set_my_short_description(short_description=SHORT_DESCRIPTION)
        await bot.set_my_description(description=DESCRIPTION)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось зарегистрировать меню бота: {e}")
        return

    for admin_id in admin_ids:
        try:
            # Отдельный список с /admin только в личном чате этого админа.
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:  # например, админ ещё не писал боту: чата нет
            logger.warning(f"⚠️ Не удалось задать админское меню для {admin_id}: {e}")
    logger.info("✅ Bot menu registered")
