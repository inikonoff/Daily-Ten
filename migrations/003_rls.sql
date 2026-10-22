-- Supabase публикует таблицы схемы public через REST (Data API) по anon-ключу.
-- Бот ходит в Postgres напрямую ролью postgres (она обходит RLS), а REST ему
-- не нужен, поэтому включаем RLS без политик: anon/authenticated ничего не
-- увидят. На обычном Postgres (Docker) миграция безвредна. Идемпотентна.

ALTER TABLE users      ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages   ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs ENABLE ROW LEVEL SECURITY;
