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
    BACKGROUND_MAX_BACKOFF_SECONDS,
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
    delivered = 0
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
            if await deliver_summary(bot, session, notify_on_fail=False):
                delivered += 1
        except Exception as e:
            logger.error(f"❌ Timeout summary failed for session {session['id']}: {e}")
    if closed:
        logger.info(f"⏱ Background pass: closed {len(closed)} session(s), summaries delivered: {delivered}")
    return len(closed)


async def background_loop(bot: Bot) -> None:
    failures = 0
    while True:
        try:
            await close_stale_sessions_once(bot)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failures += 1
            logger.error(f"❌ Background loop error (#{failures} in a row): {e}")
        # При подряд идущих ошибках (например, БД недоступна) пауза растёт 1, 2, 4, ... минут.
        delay = BACKGROUND_INTERVAL_SECONDS
        if failures:
            delay = min(BACKGROUND_INTERVAL_SECONDS, BACKGROUND_MAX_BACKOFF_SECONDS, 60 * 2 ** (failures - 1))
        await asyncio.sleep(delay)
