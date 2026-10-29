"""
События LLM-слоя для наблюдаемости (таблица generation_events, миграция 005).

record() не блокирует ответ юзеру: запись в БД уходит фоновой задачей, а
ошибки записи глотаются (метрика не должна ломать разговор). Без пула БД
(unit-тесты) — только лог.
"""

import asyncio
import logging
from typing import Optional, Set

from src.db import db

logger = logging.getLogger(__name__)

DETAIL_CHARS = 300
RAW_CHARS = 500

_pending: Set[asyncio.Task] = set()


def record(
    label: str,
    event: str,
    *,
    model: Optional[str] = None,
    detail: Optional[str] = None,
    prompt_chars: Optional[int] = None,
    raw: Optional[str] = None,
) -> None:
    logger.info(f"📈 {label}/{event} model={model} detail={(detail or '')[:120]!r}")
    if db.pool is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _insert(
            label, event, model,
            detail[:DETAIL_CHARS] if detail else None,
            prompt_chars,
            raw[:RAW_CHARS] if raw else None,
        )
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _insert(label, event, model, detail, prompt_chars, raw) -> None:
    try:
        await db.log_generation_event(label, event, model, detail, prompt_chars, raw)
    except Exception as e:
        logger.debug(f"generation event not stored: {e}")


async def flush() -> None:
    """Дождаться незаписанных событий (при остановке бота и в тестах)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
