"""
Фоновая задача (единственное исключение из «scheduler не нужен», см. PLAN.md):
раз в ~30 минут закрывает брошенные сессии (48 ч без активности). Если пар
>= 3, генерирует SS/SS4T и proactively отправляет SS обычным сообщением бота
(это и есть уведомление). Меньше 3 пар — закрытие молча.
"""

import asyncio
import logging

from aiogram import Bot

from src.config import (
    BACKGROUND_INTERVAL_SECONDS,
    MIN_PAIRS_FOR_TIMEOUT_SUMMARY,
    SESSION_TIMEOUT_HOURS,
)
from src.db import db
from src.summary import deliver_summary

logger = logging.getLogger(__name__)


async def close_stale_sessions_once(bot: Bot) -> int:
    """Один проход. Возвращает число закрытых сессий."""
    # UPDATE ... RETURNING внутри: каждую сессию обработает ровно один проход.
    closed = await db.close_stale_sessions(SESSION_TIMEOUT_HOURS)
    for session in closed:
        logger.info(
            f"⏱ Session {session['id']} (user {session['user_id']}) closed by timeout, "
            f"pairs={session['pairs_done']}"
        )
        if session["pairs_done"] < MIN_PAIRS_FOR_TIMEOUT_SUMMARY:
            continue
        try:
            # Ошибки отправки (бот заблокирован) ловятся внутри; сессия уже закрыта.
            # При провале генерации юзеру не пишем: SS повторится по /summary.
            await deliver_summary(bot, session, notify_on_fail=False)
        except Exception as e:
            logger.error(f"❌ Timeout summary failed for session {session['id']}: {e}")
    return len(closed)


async def background_loop(bot: Bot) -> None:
    while True:
        try:
            await close_stale_sessions_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ Background loop error: {e}")
        await asyncio.sleep(BACKGROUND_INTERVAL_SECONDS)
