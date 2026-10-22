-- Daily Ten — сессии по 10 обменов, Session Summary (SS) и SS4T.
-- Идемпотентна: применима и к пустой схеме (после 001), и к существующей.
--   psql "$DATABASE_URL" -f migrations/002_sessions.sql

CREATE TABLE IF NOT EXISTS sessions (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'completed')),
    completion_reason       TEXT CHECK (completion_reason IN ('finished', 'timeout')),
    pairs_done              INTEGER NOT NULL DEFAULT 0,
    non_answer_streak       INTEGER NOT NULL DEFAULT 0,   -- не-ответов подряд (потолок в config)
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    started_date            DATE NOT NULL,                -- дата старта по Europe/Minsk
    user_summary            TEXT,                         -- SS: JSON-строка (голос + карточка)
    teacher_summary         JSONB                         -- SS4T: не показывается юзеру
);

-- Не более одной активной сессии на юзера: защита от гонок.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_active_per_user
    ON sessions (user_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_sessions_user_started
    ON sessions (user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_active_activity
    ON sessions (last_activity_at) WHERE status = 'active';

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS session_id BIGINT REFERENCES sessions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS counted    BOOLEAN NOT NULL DEFAULT false,  -- считался ли ответом
    ADD COLUMN IF NOT EXISTS word_count INTEGER;                         -- для голосовых по транскрипции

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at);

ALTER TABLE error_logs
    ADD COLUMN IF NOT EXISTS session_id BIGINT REFERENCES sessions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_error_logs_session
    ON error_logs (session_id);

-- Суточный счётчик пар заменён логикой сессий.
ALTER TABLE users
    DROP COLUMN IF EXISTS daily_pairs_used,
    DROP COLUMN IF EXISTS daily_reset_date;
