-- Наблюдаемость LLM-слоя: события генерации (ошибки, переход на запасную
-- модель, упрощённый промпт Summary, сокращение реплик, не-ответы, TTS).
-- Агрегируется в админке («Generation, 24h»). Старше 30 дней удаляется
-- фоновой задачей. Идемпотентна.

CREATE TABLE IF NOT EXISTS generation_events (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    label           TEXT NOT NULL,   -- generate_reply / summary / translate / tts / stt
    event           TEXT NOT NULL,   -- error / fallback_used / lite_prompt / short_voice / ...
    model           TEXT,
    detail          TEXT,            -- текст ошибки или пояснение (обрезано)
    prompt_chars    INTEGER,
    raw_snippet     TEXT             -- начало сырого ответа модели при провале разбора
);

CREATE INDEX IF NOT EXISTS idx_generation_events_created
    ON generation_events (created_at DESC);

-- Как и остальные таблицы: не отдавать через Supabase REST по anon-ключу.
ALTER TABLE generation_events ENABLE ROW LEVEL SECURITY;
