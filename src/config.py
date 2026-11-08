from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    GROQ_API_KEYS: str = ""
    DATABASE_URL: str
    ADMIN_IDS: str = ""  # Telegram user id админов через запятую
    PORT: int = 10000

    # Модели Groq (смена модели = смена переменной окружения, без правки кода)
    # По умолчанию только дешёвая gpt-oss-20b (экономия, пока бот бесплатный). Запасной
    # модели нет: 120b на практике хуже справляется с рассуждением и JSON.
    GROQ_MODEL: str = "openai/gpt-oss-20b"
    GROQ_MODEL_FALLBACK: str = ""                     # пусто = без запасной модели
    GROQ_MODEL_LIGHT: str = "openai/gpt-oss-20b"      # простые задачи (перевод)
    GROQ_TTS_MODEL: str = "canopylabs/orpheus-v1-english"
    GROQ_STT_MODEL: str = "whisper-large-v3-turbo"
    # Глубина «рассуждения» моделей gpt-oss: low, medium, high. Рассуждение тратит
    # токены ответа, поэтому по умолчанию low. Пусто = параметр не передаётся
    # (нужно, если GROQ_MODEL не из семейства gpt-oss).
    GROQ_REASONING_EFFORT: str = "low"
    # Значения по умолчанию для настроек, которые админ переключает в боте (/admin → Settings):
    ENABLE_TTS: bool = True   # false: голосовые ответы и озвучка Summary отключены, везде текст
    STT_PROVIDER: str = "auto"  # auto = сначала Google, при неудаче Whisper; google; groq

    # Объём контекста в промпте МС (можно крутить без правки кода, см. README)
    CONTEXT_MESSAGES: int = 24  # сообщений текущей сессии; 24 = вся сессия (ТЗ §5)
    SS4T_CONTEXT: int = 3       # сколько последних SS4T (компактно) идёт в промпт

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def groq_api_keys_list(self) -> List[str]:
        return [k.strip() for k in self.GROQ_API_KEYS.split(",") if k.strip()]


settings = Settings()


def parse_admin_ids(raw: str) -> List[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def get_admin_ids() -> List[int]:
    return parse_admin_ids(settings.ADMIN_IDS)


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
SS4T_CONTEXT = settings.SS4T_CONTEXT          # env SS4T_CONTEXT, по умолчанию 3
CONTEXT_MESSAGES = settings.CONTEXT_MESSAGES  # env CONTEXT_MESSAGES, по умолчанию 24 (вся сессия)
GENERATION_EVENTS_RETENTION_DAYS = 30

# ─── Фоновая задача ─────────────────────────────────────────────────────────
BACKGROUND_INTERVAL_SECONDS = 30 * 60   # поиск брошенных сессий
BACKGROUND_MAX_BACKOFF_SECONDS = 30 * 60  # потолок паузы при подряд идущих ошибках цикла

# ─── Защита LLM-слоя ────────────────────────────────────────────────────────
REPLY_WORD_LIMIT = 55            # лимит слов реплики МС (озвучка), см. PERSONA_PROMPT.md
REPLY_WORD_TOLERANCE = 1.2       # до 55*1.2 слов принимаем как есть, дальше сокращаем
MAX_USER_MESSAGE_CHARS = 1500    # длиннее — в промпт идёт обрезанный текст
MIN_SUMMARY_VOICE_WORDS = 40     # короче — голосовое SS считается обрывом (Groq-баг)

# Лимиты токенов ответа. У gpt-oss в них входит и скрытое рассуждение, поэтому
# запас большой: при малом лимите Groq отвечает 400 json_validate_failed
# («max completion tokens reached before generating a valid document»).
REPLY_MAX_TOKENS = 1500
SUMMARY_MAX_TOKENS = 4000
TRANSLATE_MAX_TOKENS = 1000

# ─── Распознавание речи ─────────────────────────────────────────────────────
GOOGLE_STT_MAX_SECONDS = 55      # дольше Google не принимает: такие голосовые идут в Whisper
GOOGLE_STT_TIMEOUT_SECONDS = 10
FFMPEG_TIMEOUT_SECONDS = 20
SETTINGS_CACHE_SECONDS = 30      # как долго настройки из БД кэшируются в памяти

# ─── Burst rate-limit (защита от спама) ─────────────────────────────────────
RATE_LIMIT_WINDOW = 60       # секунд для подсчёта сообщений
RATE_LIMIT_MAX = 10          # макс сообщений за WINDOW для обычного юзера
RATE_LIMIT_MAX_ADMIN = 100
COOLDOWN_DURATION = 60       # секунд тишины после превышения лимита
WARN_THRESHOLD = 7
