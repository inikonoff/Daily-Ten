import logging
import json
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import asyncpg

from src.config import (
    settings,
    RATE_LIMIT_WINDOW,
    RATE_LIMIT_MAX,
    RATE_LIMIT_MAX_ADMIN,
    COOLDOWN_DURATION,
    WARN_THRESHOLD,
)

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        kwargs = {}
        if urlparse(settings.DATABASE_URL).port == 6543:
            # Supabase transaction pooler (pgbouncer) не поддерживает prepared statements.
            kwargs["statement_cache_size"] = 0
        self.pool = await asyncpg.create_pool(
            settings.DATABASE_URL, min_size=1, max_size=5, init=self._init_connection, **kwargs
        )
        logger.info("✅ Postgres pool connected")

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        # jsonb ⇄ dict (teacher_summary), без ручного json.dumps/loads в запросах
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"❌ DB ping failed: {e}")
            return False

    # ─── Users ──────────────────────────────────────────────────────────────

    async def get_or_create_user(self, telegram_id: int) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1", telegram_id
            )
            if row:
                await conn.execute(
                    "UPDATE users SET last_active = now() WHERE telegram_id = $1",
                    telegram_id,
                )
                return dict(row)

            row = await conn.fetchrow(
                "INSERT INTO users (telegram_id) VALUES ($1) RETURNING *",
                telegram_id,
            )
            logger.info(f"👤 New user {telegram_id}")
            return dict(row)

    async def set_name(self, telegram_id: int, name: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET name = $1 WHERE telegram_id = $2", name, telegram_id
            )

    async def set_level(self, telegram_id: int, level: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET level = $1 WHERE telegram_id = $2", level, telegram_id
            )

    # ─── Burst rate-limit (защита от спама, персистентно — см. PLAN.md) ─────

    async def check_burst(self, telegram_id: int, is_admin: bool) -> Dict[str, Any]:
        """
        Возвращает {"blocked": bool, "warn": bool}. blocked=True — сообщение
        нужно молча проигнорировать (юзер уже предупреждён при входе в
        cooldown). warn=True — сообщение обработать, но сначала предупредить
        юзера о скорости.
        """
        now = datetime.now(timezone.utc)
        limit = RATE_LIMIT_MAX_ADMIN if is_admin else RATE_LIMIT_MAX

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT burst_message_count, burst_window_start, burst_cooldown_until "
                "FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            cooldown_until = row["burst_cooldown_until"]
            if not is_admin and cooldown_until and now < cooldown_until:
                return {"blocked": True, "warn": False}

            window_start = row["burst_window_start"]
            count = row["burst_message_count"] or 0

            if not window_start or (now - window_start).total_seconds() > RATE_LIMIT_WINDOW:
                window_start = now
                count = 0

            count += 1

            if count > limit and not is_admin:
                new_cooldown = now + timedelta(seconds=COOLDOWN_DURATION)
                await conn.execute(
                    "UPDATE users SET burst_message_count = $2, burst_window_start = $3, "
                    "burst_cooldown_until = $4 WHERE telegram_id = $1",
                    telegram_id, count, window_start, new_cooldown,
                )
                return {"blocked": True, "warn": False}

            await conn.execute(
                "UPDATE users SET burst_message_count = $2, burst_window_start = $3, "
                "burst_cooldown_until = NULL WHERE telegram_id = $1",
                telegram_id, count, window_start,
            )
            return {"blocked": False, "warn": (count == WARN_THRESHOLD and not is_admin)}

    # ─── Sessions («десятка», см. PLAN.md) ──────────────────────────────────

    async def create_session(self, user_id: int, started_date: date) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO sessions (user_id, started_date) VALUES ($1, $2) RETURNING *",
                user_id, started_date,
            )
            return dict(row)

    async def get_active_session(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE user_id = $1 AND status = 'active'", user_id
            )
            return dict(row) if row else None

    async def get_last_session(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE user_id = $1 ORDER BY started_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
            return dict(row) if row else None

    async def advance_session(self, session_id: int, pairs_done: int, non_answer_streak: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET pairs_done = $2, non_answer_streak = $3, "
                "last_activity_at = now() WHERE id = $1",
                session_id, pairs_done, non_answer_streak,
            )

    async def finish_session(
        self, session_id: int, pairs_done: int, reason: str = "finished"
    ) -> Optional[Dict[str, Any]]:
        """
        Закрывает активную сессию (finished — все 10 пар, early — юзер завершил
        сам). Атомарно: вернёт строку только тому вызову, который реально
        закрыл сессию.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE sessions SET status = 'completed', completion_reason = $3, "
                "pairs_done = $2, non_answer_streak = 0, last_activity_at = now(), "
                "completed_at = now() WHERE id = $1 AND status = 'active' RETURNING *",
                session_id, pairs_done, reason,
            )
            return dict(row) if row else None

    async def close_stale_sessions(self, timeout_hours: int) -> List[Dict[str, Any]]:
        """
        Фоновая задача: закрывает брошенные сессии (completion_reason = timeout).
        UPDATE ... RETURNING — каждую сессию обработает ровно один вызов.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE sessions SET status = 'completed', completion_reason = 'timeout', "
                "completed_at = now() WHERE status = 'active' "
                "AND last_activity_at < now() - make_interval(hours => $1) RETURNING *",
                timeout_hours,
            )
            return [dict(r) for r in rows]

    async def save_summary(
        self, session_id: int, user_summary: str, teacher_summary: Dict[str, Any]
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET user_summary = $2, teacher_summary = $3 WHERE id = $1",
                session_id, user_summary, teacher_summary,
            )

    async def get_latest_summarized_session(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE user_id = $1 AND user_summary IS NOT NULL "
                "ORDER BY completed_at DESC NULLS LAST LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def get_recent_teacher_summaries(self, user_id: int, limit: int) -> List[Dict[str, Any]]:
        """Последние SS4T юзера, новые первыми — память МС между сессиями."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT teacher_summary FROM sessions WHERE user_id = $1 "
                "AND teacher_summary IS NOT NULL ORDER BY completed_at DESC NULLS LAST LIMIT $2",
                user_id, limit,
            )
            return [r["teacher_summary"] for r in rows]

    # ─── Messages (история сессии + Text/Translate после рестарта) ──────────

    async def save_message(
        self,
        user_id: int,
        role: str,
        text: str,
        session_id: Optional[int] = None,
        counted: bool = False,
        word_count: Optional[int] = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO messages (user_id, role, text, session_id, counted, word_count) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                user_id, role, text, session_id, counted, word_count,
            )
            return row["id"]

    async def set_message_telegram_id(self, message_id: int, telegram_message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET telegram_message_id = $1 WHERE id = $2",
                telegram_message_id, message_id,
            )

    async def get_message_by_telegram_id(self, user_id: int, telegram_message_id: int) -> Optional[str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text FROM messages WHERE user_id = $1 AND telegram_message_id = $2",
                user_id, telegram_message_id,
            )
            return row["text"] if row else None

    async def get_session_dialog(self, session_id: int, limit: int) -> List[Dict[str, str]]:
        """Последние `limit` сообщений сессии в хронологическом порядке — контекст промпта."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, text FROM ("
                "  SELECT role, text, created_at, id FROM messages WHERE session_id = $1 "
                "  ORDER BY created_at DESC, id DESC LIMIT $2"
                ") t ORDER BY created_at, id",
                session_id, limit,
            )
            return [{"role": r["role"], "content": r["text"]} for r in rows]

    async def get_session_answer_stats(self, session_id: int) -> Dict[str, Any]:
        """Средняя длина засчитанных ответов юзера в сессии (слов)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n, coalesce(avg(word_count), 0)::float AS avg_words "
                "FROM messages WHERE session_id = $1 AND role = 'user' AND counted",
                session_id,
            )
            return {"answers": row["n"], "avg_words": round(row["avg_words"], 1)}

    # ─── Error log (для SS и админ-панели) ──────────────────────────────────

    async def log_error(
        self,
        user_id: int,
        category: str,
        mistake: str,
        corrected: str,
        session_id: Optional[int] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO error_logs (user_id, category, mistake_text, corrected_text, session_id) "
                "VALUES ($1, $2, $3, $4, $5)",
                user_id, category, mistake, corrected, session_id,
            )

    async def get_session_errors(self, session_id: int, limit: int = 30) -> List[Dict[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT category, mistake_text, corrected_text FROM error_logs "
                "WHERE session_id = $1 ORDER BY created_at LIMIT $2",
                session_id, limit,
            )
            return [dict(r) for r in rows]

    # ─── Админ-панель ────────────────────────────────────────────────────────

    async def get_admin_stats(self) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT count(*) FROM users")
            active_today = await conn.fetchval(
                "SELECT count(*) FROM users WHERE last_active >= date_trunc('day', now())"
            )
            active_week = await conn.fetchval(
                "SELECT count(*) FROM users WHERE last_active >= now() - interval '7 days'"
            )
            messages_today = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE created_at >= date_trunc('day', now())"
            )
            level_rows = await conn.fetch(
                "SELECT coalesce(level, 'unknown') AS level, count(*) AS n "
                "FROM users GROUP BY level ORDER BY n DESC"
            )
            return {
                "total_users": total_users,
                "active_today": active_today,
                "active_week": active_week,
                "messages_today": messages_today,
                "levels": {r["level"]: r["n"] for r in level_rows},
            }

    async def get_session_stats(self) -> Dict[str, Any]:
        """Сессии: сколько всего, доля завершённых против таймаутов."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE status = 'active') AS active, "
                "count(*) FILTER (WHERE completion_reason = 'finished') AS finished, "
                "count(*) FILTER (WHERE completion_reason = 'early') AS early, "
                "count(*) FILTER (WHERE completion_reason = 'timeout') AS timed_out, "
                "count(*) FILTER (WHERE user_summary IS NOT NULL) AS with_summary "
                "FROM sessions"
            )
            return dict(row)

    async def get_avg_words_by_session_ordinal(self, max_ordinal: int = 10) -> List[Dict[str, Any]]:
        """
        Главная дипломная метрика: средняя длина ответа юзера (слов) по
        порядковому номеру сессии юзера (1-я, 2-я, ...) — «разговорился ли».
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "WITH ranked AS ("
                "  SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY started_at) AS ordinal "
                "  FROM sessions"
                ") "
                "SELECT r.ordinal, avg(m.word_count)::float AS avg_words, "
                "count(DISTINCT m.session_id) AS sessions, count(*) AS answers "
                "FROM messages m JOIN ranked r ON r.id = m.session_id "
                "WHERE m.role = 'user' AND m.counted AND m.word_count IS NOT NULL "
                "AND r.ordinal <= $1 GROUP BY r.ordinal ORDER BY r.ordinal",
                max_ordinal,
            )
            return [dict(r) for r in rows]

    async def get_error_category_breakdown(self, limit: int = 10) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT category, count(*) AS n FROM error_logs "
                "GROUP BY category ORDER BY n DESC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]

    async def get_user_card(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
            if not user:
                return None
            message_count = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE user_id = $1", telegram_id
            )
            recent_errors = await conn.fetch(
                "SELECT category, mistake_text, corrected_text, created_at FROM error_logs "
                "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 5",
                telegram_id,
            )
            sessions = await conn.fetch(
                "SELECT id, status, completion_reason, pairs_done, started_at, "
                "teacher_summary->>'level_estimate' AS level_estimate FROM sessions "
                "WHERE user_id = $1 ORDER BY started_at DESC LIMIT 5",
                telegram_id,
            )
            session_total = await conn.fetchval(
                "SELECT count(*) FROM sessions WHERE user_id = $1", telegram_id
            )
            return {
                "user": dict(user),
                "message_count": message_count,
                "recent_errors": [dict(r) for r in recent_errors],
                "recent_sessions": [dict(r) for r in sessions],
                "session_total": session_total,
            }

    async def list_users(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id, name, level, last_active FROM users "
                "ORDER BY last_active DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )
            return [dict(r) for r in rows]


db = Database()
