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


async def _step(name: str, call) -> bool:
    """Каждый шаг независим: сбой одного (например, меню) не отменяет остальные (описание)."""
    try:
        await call
        return True
    except Exception as e:
        logger.warning(f"⚠️ Не удалось выполнить «{name}»: {e}")
        return False


async def setup_bot_menu(bot: Bot, admin_ids: Iterable[int]) -> None:
    results = {
        "команды": await _step("команды", bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())),
        "кнопка меню": await _step("кнопка меню", bot.set_chat_menu_button(menu_button=MenuButtonCommands())),
        "короткое описание": await _step(
            "короткое описание", bot.set_my_short_description(short_description=SHORT_DESCRIPTION)
        ),
        "описание": await _step("описание", bot.set_my_description(description=DESCRIPTION)),
    }
    for admin_id in admin_ids:
        # Отдельный список с /admin только в личном чате этого админа
        # (не сработает, если админ ещё не писал боту: чата нет).
        results[f"админ {admin_id}"] = await _step(
            f"админское меню {admin_id}",
            bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)),
        )
    done = [name for name, ok in results.items() if ok]
    failed = [name for name, ok in results.items() if not ok]
    logger.info(f"✅ Bot menu: выполнено {done}" + (f", НЕ выполнено {failed}" if failed else ""))
