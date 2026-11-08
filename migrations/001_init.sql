-- Daily Ten: схема базы данных.
--
-- Применяется одинаково локально (Docker Postgres) и на Supabase:
--   psql "$DATABASE_URL" -f migrations/001_init.sql
--
-- Скрипт идемпотентен (можно запускать повторно). Следующие изменения схемы
-- добавляются отдельными файлами 002_*.sql, 003_*.sql и так далее.

-- ─── Пользователи ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    telegram_id             BIGINT PRIMARY KEY,
    name                    TEXT,
    level                   TEXT,                 -- самооценка из онбординга: beginner / intermediate / advanced
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- burst rate-limit (защита от спама), хранится в БД, а не в памяти процесса
    burst_message_count     INTEGER NOT NULL DEFAULT 0,
    burst_window_start      TIMESTAMPTZ,
    burst_cooldown_until    TIMESTAMPTZ
);

-- ─── Сессии («десятки») ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'completed')),
    completion_reason       TEXT
                            CHECK (completion_reason IN ('finished', 'timeout', 'early')),
    pairs_done              INTEGER NOT NULL DEFAULT 0,
    non_answer_streak       INTEGER NOT NULL DEFAULT 0,   -- не-ответов подряд
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    started_date            DATE NOT NULL,                -- дата старта по Europe/Minsk
    user_summary            TEXT,                         -- Session Summary: JSON-строка (голос + карточка)
    teacher_summary         JSONB                         -- SS4T, юзеру не показывается
);

-- Не более одной активной сессии на юзера: защита от гонок.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_active_per_user
    ON sessions (user_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_sessions_user_started
    ON sessions (user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_active_activity
    ON sessions (last_activity_at) WHERE status = 'active';

-- ─── Сообщения ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    session_id              BIGINT REFERENCES sessions(id) ON DELETE SET NULL,
    role                    TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text                    TEXT NOT NULL,
    telegram_message_id     BIGINT,                       -- для кнопок Text/Translate после рестарта
    counted                 BOOLEAN NOT NULL DEFAULT false,  -- считался ли ответом
    word_count              INTEGER,                      -- длина ответа в словах (для голосовых по транскрипции)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_user_created
    ON messages (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_telegram_message_id
    ON messages (user_id, telegram_message_id);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at);

-- ─── Ошибки юзера (фиксируются молча, разбираются в Summary) ────────────────
CREATE TABLE IF NOT EXISTS error_logs (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    session_id              BIGINT REFERENCES sessions(id) ON DELETE SET NULL,
    category                TEXT NOT NULL,                -- grammar / vocabulary / prepositions / structure
    mistake_text            TEXT NOT NULL,
    corrected_text          TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_error_logs_user_created
    ON error_logs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_error_logs_category
    ON error_logs (category);

CREATE INDEX IF NOT EXISTS idx_error_logs_session
    ON error_logs (session_id);

-- ─── События LLM-слоя (наблюдаемость, блок «Generation» в админке) ───────────
CREATE TABLE IF NOT EXISTS generation_events (
    id                      BIGSERIAL PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    label                   TEXT NOT NULL,                -- generate_reply / summary / translate / tts / stt
    event                   TEXT NOT NULL,                -- error / bad_json / fallback_used / stt_ok / ...
    model                   TEXT,
    detail                  TEXT,                         -- текст ошибки или пояснение (обрезано)
    prompt_chars            INTEGER,
    raw_snippet             TEXT                          -- начало сырого ответа модели при провале разбора
);

CREATE INDEX IF NOT EXISTS idx_generation_events_created
    ON generation_events (created_at DESC);

-- ─── Настройки, меняемые из админки без рестарта ────────────────────────────
CREATE TABLE IF NOT EXISTS app_settings (
    key                     TEXT PRIMARY KEY,
    value                   TEXT NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by              BIGINT
);

-- ─── Безопасность ───────────────────────────────────────────────────────────
-- Supabase публикует таблицы схемы public через REST (Data API) по anon-ключу.
-- Бот ходит в Postgres напрямую ролью postgres (она обходит RLS), а REST ему
-- не нужен, поэтому включаем RLS без политик: anon и authenticated ничего не
-- увидят. На обычном Postgres (Docker) это безвредно.
ALTER TABLE users             ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages          ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE generation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_settings      ENABLE ROW LEVEL SECURITY;
