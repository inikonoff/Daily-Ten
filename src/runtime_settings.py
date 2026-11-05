"""
Настройки, которые админ меняет прямо в боте (/admin → Settings), без
рестарта. Значения по умолчанию берутся из окружения (config.Settings), а
значение из таблицы app_settings их перекрывает. Читается с кэшем в памяти,
чтобы не ходить в БД на каждое голосовое; запись обновляет кэш сразу.
Без пула БД (unit-тесты) или при ошибке чтения работают значения по умолчанию.
"""

import logging
import time
from typing import Dict, Optional

from src.config import SETTINGS_CACHE_SECONDS, settings
from src.db import db

logger = logging.getLogger(__name__)

STT_PROVIDER_KEY = "stt_provider"
TTS_ENABLED_KEY = "tts_enabled"

STT_AUTO = "auto"      # сначала Google, при неудаче Whisper
STT_GOOGLE = "google"  # только Google (длинные записи всё равно идут в Whisper)
STT_GROQ = "groq"      # только Whisper на Groq
STT_PROVIDERS = (STT_AUTO, STT_GOOGLE, STT_GROQ)

_cache: Dict[str, str] = {}
_loaded_at: float = 0.0


def _default(key: str) -> str:
    if key == STT_PROVIDER_KEY:
        value = settings.STT_PROVIDER.strip().lower()
        return value if value in STT_PROVIDERS else STT_AUTO
    if key == TTS_ENABLED_KEY:
        return "true" if settings.ENABLE_TTS else "false"
    raise KeyError(key)


async def _refresh(force: bool = False) -> None:
    global _cache, _loaded_at
    if db.pool is None:
        return
    if not force and time.monotonic() - _loaded_at < SETTINGS_CACHE_SECONDS:
        return
    try:
        _cache = await db.get_app_settings()
        _loaded_at = time.monotonic()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось прочитать app_settings, работают значения по умолчанию: {e}")
        _loaded_at = time.monotonic()  # не долбить БД при каждом запросе, пока она недоступна


async def get(key: str) -> str:
    await _refresh()
    return _cache.get(key, _default(key))


async def set_value(key: str, value: str, updated_by: Optional[int] = None) -> None:
    _default(key)  # неизвестный ключ = KeyError
    await db.set_app_setting(key, value, updated_by)
    _cache[key] = value


async def stt_provider() -> str:
    value = (await get(STT_PROVIDER_KEY)).strip().lower()
    return value if value in STT_PROVIDERS else STT_AUTO


async def tts_enabled() -> bool:
    return (await get(TTS_ENABLED_KEY)).strip().lower() in ("1", "true", "yes", "on")


def reset_cache() -> None:
    """Для тестов."""
    global _cache, _loaded_at
    _cache, _loaded_at = {}, 0.0
