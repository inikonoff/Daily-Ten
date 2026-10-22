-- Daily Ten — начальная схема.
-- Применяется одинаково локально (Docker Postgres) и на Supabase:
--   psql "$DATABASE_URL" -f migrations/001_init.sql

CREATE TABLE IF NOT EXISTS users (
    telegram_id             BIGINT PRIMARY KEY,
    name                    TEXT,
    level                   TEXT,                 -- beginner / intermediate / advanced
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- дневной лимит: 10 пар сообщений/сутки (см. PLAN.md)
    daily_pairs_used        INTEGER NOT NULL DEFAULT 0,
    daily_reset_date        DATE NOT NULL DEFAULT CURRENT_DATE,

    -- burst rate-limit (защита от спама) — персистентно с первого дня,
    -- не в памяти процесса (см. PLAN.md, урок из speechflow)
    burst_message_count     INTEGER NOT NULL DEFAULT 0,
    burst_window_start      TIMESTAMPTZ,
    burst_cooldown_until    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    role                    TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text                    TEXT NOT NULL,
    telegram_message_id     BIGINT,                -- для кнопок Text/Translate после рестарта
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_user_created
    ON messages (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_telegram_message_id
    ON messages (user_id, telegram_message_id);

CREATE TABLE IF NOT EXISTS error_logs (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    category                TEXT NOT NULL,         -- grammar / vocabulary / prepositions / structure
    mistake_text            TEXT NOT NULL,
    corrected_text          TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_error_logs_user_created
    ON error_logs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_error_logs_category
    ON error_logs (category);
