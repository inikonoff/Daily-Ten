-- Настройки, которые админ меняет в боте без рестарта (режим распознавания
-- речи, озвучка). Значения из окружения остаются значениями по умолчанию.
-- Идемпотентна.

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  BIGINT
);

-- Как и остальные таблицы: не отдавать через Supabase REST по anon-ключу.
ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;
