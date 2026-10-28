"""
Чистая логика сессии «десятки» — без БД и Telegram, чтобы её можно было
проверять отдельно. Режим реплики выбирает код по счётчику, а не LLM
(иначе МС может попрощаться раньше времени), см. PLAN.md.
"""

import re
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


def truncate_message(text: str, limit: int) -> str:
    """Обрезает слишком длинное сообщение юзера по границе слова (для промпта и истории)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= int(limit * 0.8):
        cut = cut[:space]
    return cut.rstrip()


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def shorten_reply(text: str, limit: int) -> str:
    """
    Сокращает реплику, превысившую лимит слов, не трогая первую (реакция) и
    последнюю (приглашение продолжить или прощание) фразы: убираются средние
    предложения. Если фраз две и меньше, текст остаётся как есть.
    """
    sentences = _SENTENCE_SPLIT.split(text.strip())
    while word_count(" ".join(sentences)) > limit and len(sentences) > 2:
        del sentences[1]
    return " ".join(sentences)

