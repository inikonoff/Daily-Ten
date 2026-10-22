from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    GROQ_API_KEYS: str = ""
    DATABASE_URL: str
    PORT: int = 10000

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def groq_api_keys_list(self) -> List[str]:
        return [k.strip() for k in self.GROQ_API_KEYS.split(",") if k.strip()]


settings = Settings()


def get_admin_ids() -> List[int]:
    import os
    raw = os.environ.get("ADMIN_IDS", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


ADMIN_IDS = get_admin_ids()

# ─── Персонаж ───────────────────────────────────────────────────────────────
VOICE = "diana"
DISPLAY_NAME = "Mrs. Smith"
PRODUCT_NAME = "Daily Ten"

# ─── Сессия «десятки» (см. PLAN.md) ─────────────────────────────────────────
SESSION_PAIRS = 10               # ответов юзера в сессии; реплик МС на одну больше
TIMEZONE = "Europe/Minsk"        # «день» считается по полуночи Минска
SESSION_TIMEOUT_HOURS = 48       # без активности → сессия закрывается (timeout)
MIN_PAIRS_FOR_TIMEOUT_SUMMARY = 3
MIN_PAIRS_TO_FINISH_EARLY = 3   # раньше кнопка «Finish» не показывается, /finish отказывает
MAX_CONSECUTIVE_NON_ANSWERS = 2  # потом следующее сообщение считается ответом
SS4T_CONTEXT = 3                 # сколько последних SS4T идёт в промпт МС
CONTEXT_MESSAGES = 24            # потолок сообщений текущей сессии в промпте (~22 + запас)

# ─── Фоновая задача ─────────────────────────────────────────────────────────
BACKGROUND_INTERVAL_SECONDS = 30 * 60   # поиск брошенных сессий

# ─── Burst rate-limit (защита от спама) ─────────────────────────────────────
RATE_LIMIT_WINDOW = 60       # секунд для подсчёта сообщений
RATE_LIMIT_MAX = 10          # макс сообщений за WINDOW для обычного юзера
RATE_LIMIT_MAX_ADMIN = 100
COOLDOWN_DURATION = 60       # секунд тишины после превышения лимита
WARN_THRESHOLD = 7
