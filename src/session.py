"""
Чистая логика сессии «десятки» — без БД и Telegram, чтобы её можно было
проверять отдельно. Режим реплики выбирает код по счётчику, а не LLM
(иначе МС может попрощаться раньше времени), см. PLAN.md.
"""

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.config import SESSION_PAIRS, TIMEZONE

OPENING = "opening"    # реплика 1: открывает сессию, юзер ещё ничего не сказал
NORMAL = "normal"      # реплики 2..9
PREFINAL = "prefinal"  # реплика 10 (после 9-го ответа)
CLOSING = "closing"    # реплика 11 (после 10-го ответа): прощание, затем SS
EARLY = "early"        # юзер сам завершил сессию досрочно: прощание, затем SS

_TZ = ZoneInfo(TIMEZONE)


def reply_number(pairs_done: int) -> int:
    """Номер реплики МС после `pairs_done` засчитанных ответов (открытие = 1)."""
    return pairs_done + 1


def mode_for_reply(reply_no: int) -> str:
    if reply_no <= 1:
        return OPENING
    if reply_no <= SESSION_PAIRS - 1:
        return NORMAL
    if reply_no == SESSION_PAIRS:
        return PREFINAL
    return CLOSING


def minsk_date(moment: Optional[datetime] = None) -> date:
    """Календарная дата по Минску (полночь Минска — граница дня)."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ).date()


def word_count(text: str) -> int:
    return len(text.split())
