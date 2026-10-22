-- Досрочное завершение сессии («Finish the session»): новая причина
-- completion_reason = 'early'. Идемпотентна.

ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_completion_reason_check;
ALTER TABLE sessions ADD CONSTRAINT sessions_completion_reason_check
    CHECK (completion_reason IN ('finished', 'timeout', 'early'));
