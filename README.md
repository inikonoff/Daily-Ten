# Daily Ten

Telegram-бот: каждый день один разговор с учительницей Mrs. Smith из **10 обменов
репликами**, в конце — **Session Summary** (голосовое + текстовая карточка
с разбором английского). В разговоре коррекции нет: ошибки собираются молча
и разбираются только в Summary. Следующие сессии МС строит на памяти о
прошлых. Решения и обоснование — в [`PLAN.md`](PLAN.md) и
[`PERSONA_PROMPT.md`](PERSONA_PROMPT.md).

## Как это работает

- `/start` — онбординг (имя + уровень) и сразу первая десятка; повторно
  показывает статус. `/ten` или кнопка «Start the ten» — новая сессия.
  `/summary` — последняя Session Summary. `/finish` или кнопка
  «🏁 Finish the session» (с 3-й пары) — завершить сессию досрочно: МС
  прощается и присылает Summary по тому, что есть.
- Реплики МС 1–9 обычные, 10-я — предфинальная, 11-я (после 10-го ответа)
  — прощание; сразу за ней приходит Summary. Режим выбирает код по
  счётчику, текст пишет LLM. Под каждой репликой полоска `●●●○○○○○○○`
  (в голосовых — в подписи).
- Недоделанная сессия переходит через полночь; новая десятка — не раньше
  следующих суток по Минску. Брошенная сессия закрывается через 48 часов,
  при ≥ 3 парах юзеру приходит Summary.
- Голос — mirror: на голосовое отвечаем голосом, на текст текстом.

## Стек

- Python / [aiogram](https://docs.aiogram.dev/) 3, long polling
- Postgres ([Supabase](https://supabase.com) в проде, Docker локально) через `asyncpg`, без ORM
- Groq API (`openai/gpt-oss-20b`, запасная `gpt-oss-120b`) для ответов и Summary, распознавание речи: Google (бесплатно) и/или Whisper `whisper-large-v3-turbo` на Groq, `canopylabs/orpheus-v1-english` для TTS

## Структура

```
src/
├── main.py          # Bot/Dispatcher, health-сервер, фоновая задача, polling
├── health_server.py # /health (без БД), /health/db
├── config.py        # env-переменные, параметры сессии
├── prompt.py        # промпт МС (+ блок режима), промпт SS/SS4T
├── session.py       # чистая логика: режимы, день по Минску
├── db.py            # asyncpg pool, все SQL-запросы
├── groq_client.py   # вызовы Groq: ответ, SS+SS4T, TTS, транскрибация
├── summary.py       # генерация и доставка Session Summary
├── metrics.py       # события генерации (generation_events, админка)
├── stt.py           # распознавание голосовых: Google и/или Whisper
├── runtime_settings.py # настройки из админки (app_settings), кэш
├── background.py    # закрытие брошенных сессий (раз в 30 минут)
├── keyboards.py     # кнопки, формат полоски прогресса
├── handlers.py      # /start, /ten, /summary, машина состояний сессии
└── admin.py         # админ-панель
tests/                 # pytest (см. раздел «Тесты»)
migrations/
├── 001_init.sql
├── 002_sessions.sql
├── 003_rls.sql
├── 004_early_finish.sql
├── 005_generation_events.sql
└── 006_app_settings.sql
```

## Локальный запуск

1. Postgres в Docker:
   ```bash
   docker run -d --name daily-ten-db -e POSTGRES_PASSWORD=postgres \
     -p 5432:5432 postgres:16
   createdb -h localhost -U postgres daily_ten   # или через psql
   ```
2. Применить миграции по порядку (002 идемпотентна):
   ```bash
   for f in migrations/*.sql; do
     psql "postgresql://postgres:postgres@localhost:5432/daily_ten" -f "$f"
   done
   ```
3. `.env` из `.env.example` — заполнить `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEYS`, `DATABASE_URL` (строка выше), `ADMIN_IDS`.
4. ```bash
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   python -m src.main
   ```

## Деплой (Render, free Web Service + Supabase)

Long polling. Free Web Service засыпает после 15 минут без входящего HTTP,
а фоновая задача (закрытие брошенных сессий) должна жить, поэтому
`health_server.py` подключён к `main.py` и нужен внешний пингер:

1. На [UptimeRobot](https://uptimerobot.com) (или cron-job.org) создать
   монитор типа HTTP(s) на `https://<твой-сервис>.onrender.com/health`
   с интервалом **не реже 10–14 минут** (Render засыпает через 15). Метод
   HEAD или GET — оба отвечают 200.
2. `/health` намеренно **не обращается к БД**. Проверка БД вручную —
   `/health/db`.
3. Отдельный DB keep-alive не нужен: фоновая задача раз в 30 минут делает
   запрос к БД, это считается активностью (free-проект Supabase ставится
   на паузу после ~7 дней без неё). Если проект всё же встанет на паузу,
   снимите её в дашборде.

### Supabase

1. Создать проект, в **Project Settings → Database → Connection string**
   выбрать **Session pooler** и скопировать строку вида
   `postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
   Спецсимволы в пароле URL-кодируются (`@` → `%40`, `#` → `%23`).
2. Положить её в `DATABASE_URL` на Render. Прямой host
   `db.<ref>.supabase.co` на Render free не подходит (только IPv6).
   Transaction pooler (порт 6543) тоже работает: `db.py` сам отключает кэш
   prepared statements.
3. Применить миграции к Supabase с вашей машины:
   `for f in migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done`
   (или вставить файлы по очереди в **SQL Editor**).
4. Данные видны в **Table Editor**: `users`, `sessions`, `messages`,
   `error_logs`. Бот ходит в Postgres напрямую, `supabase-py` не нужен.
   Миграция `003_rls.sql` включает RLS без политик: иначе таблицы
   public были бы читаемы через REST-ключ anon. Боту это не мешает:
   подключение идёт ролью `postgres`, она обходит RLS.

## Модели Groq и их смена

Модели задаются переменными окружения (значения по умолчанию в
`.env.example`), код менять не нужно:

По умолчанию стоят дешёвые модели (бот пока бесплатный):

- `GROQ_MODEL` (по умолчанию `openai/gpt-oss-20b`): ответы МС и Summary;
- `GROQ_MODEL_FALLBACK` (`openai/gpt-oss-120b`): запасная, более тяжёлая
  модель, на неё бот переходит, когда основная не отвечает. Пусто = без
  запасной;
- `GROQ_MODEL_LIGHT` (`openai/gpt-oss-20b`): простые задачи, перевод;
- `GROQ_STT_MODEL` (`whisper-large-v3-turbo`), `GROQ_TTS_MODEL`:
  Whisper на Groq и озвучка;
- `ENABLE_TTS`, `STT_PROVIDER`: значения по умолчанию для настроек из
  админки (см. ниже), в боте их можно переключить без рестарта.

Как сменить модель: поменять переменную в Render (Environment) и
перезапустить сервис. Что проверить после смены: в админке
**⚠️ Generation (24h)** не растут `bad_json` и `error`, `fallback_used`
редкий; пара живых сессий звучит нормально. Чтобы сделать запасную
`gpt-oss-120b` основной (если 20b не справляется с качеством реплик),
поменяйте местами `GROQ_MODEL` и `GROQ_MODEL_FALLBACK`. Решение
принимать по данным из этого блока, а не заранее.

Объём контекста тоже настраивается: `CONTEXT_MESSAGES` (по умолчанию 24,
то есть вся сессия) и `SS4T_CONTEXT` (3). Для оценки: к последнему ходу
системный промпт занимает около 1,5k токенов, вся история около 1k, так
что урезание истории до 12 сообщений экономит примерно 400 токенов только
на поздних ходах.

### Настройки в админке и распознавание речи

`/admin → ⚙️ Settings` (только `ADMIN_IDS`), изменения вступают в силу
за секунды, без рестарта. Значения хранятся в таблице `app_settings`
(миграция 006), значения из окружения служат значениями по умолчанию.

**Режим распознавания голосовых:**

| Режим | Что делает |
|---|---|
| **Auto** (по умолчанию) | сначала Google (бесплатно); если не вышло (сеть, блокировка, речь не разобрана), запрос идёт в Whisper |
| Google only | только Google, без запасного пути |
| Whisper only | только Whisper на Groq |

Записи длиннее 55 секунд Google не принимает, такие всегда уходят в
Whisper. Там же переключатель озвучки (Voice replies ON/OFF). В экране
видны и текущие модели (меняются в Render через окружение).

Что нужно знать про Google:
- Это бесплатное неофициальное веб-API (через библиотеку
  `SpeechRecognition`), без гарантий и SLA. С IP облачных хостингов
  запросы иногда блокируются или упираются в лимиты: в режиме Auto это
  не страшно (запрос уйдёт в Whisper), в режиме Google only юзер получит
  «не расслышала». Следите за `stt_ok` / `stt_fail` в блоке Generation.
- Telegram присылает OGG/Opus, поэтому запись сначала переводится в PCM
  через ffmpeg. Бинарник ffmpeg приходит из pip-пакета `imageio-ffmpeg`
  (свой Dockerfile на Render не нужен).
- Качество: распознавание иногда «исправляет» грамматику на ходу, и тогда
  ошибки юзера не попадают в разбор. Перед тем как полагаться на Google,
  прогоните 10–20 реальных голосовых и сравните транскрипты с Whisper.

### Мониторинг генерации

Каждое значимое событие LLM-слоя пишется в таблицу `generation_events`
(миграция 005) и агрегируется в админке, блок **⚠️ Generation (24h)**:

| event | что значит |
|---|---|
| `bad_json` | ответ модели не разобрался как JSON (будет повтор) |
| `error` | вызов провалился целиком (API или JSON после повтора) |
| `fallback_used` | ответ дала запасная модель |
| `reply_shortened` | реплика длиннее лимита, код её сократил |
| `non_answer` / `forced_answer` | модель сочла сообщение не-ответом / сработал потолок 2 подряд |
| `lite_prompt` | Summary получилось только с упрощённого промпта |
| `short_voice` / `voice_dropped` | голосовой текст Summary слишком короткий / ушла только карточка |
| `tts_ok` / `tts_fail` | озвучка (в админке видна доля успехов) |
| `stt_ok` / `stt_fail` | распознавание речи; в колонке модели `google` или `whisper` |

Если Orpheus упирается в лимиты, проще всего выставить `ENABLE_TTS=false`.
События старше 30 дней удаляет фоновая задача. Лимиты и список доступных моделей смотрите в
консоли Groq. При сбоях разбора ответа в логах появляется строка
`generate_reply: model=... error=... raw=...` с началом сырого ответа.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest                                  # unit-тесты, БД не нужна
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/daily_ten_test \
  pytest                                # плюс сценарии сессии на реальном Postgres
```

В GitHub Actions тесты запускаются на каждый push и PR
(`.github/workflows/tests.yml`, с Postgres 16 в сервис-контейнере).

`TEST_DATABASE_URL` обязана указывать на отдельную базу с `test` в имени:
тесты применяют миграции и очищают таблицы. LLM, TTS и Telegram в тестах
подменены заглушками, поэтому живые ответы Groq они не проверяют.

## Админ-панель

`/admin` (только `ADMIN_IDS`): общая статистика, разбор ошибок по
категориям, **Sessions** (число сессий, доля завершённых / досрочных /
таймаутов, средняя длина ответа юзера в словах по порядковому номеру
сессии — график «разговорился ли»), карточка юзера.

## Миграции

Новая миграция — следующий номер в `migrations/` (`003_...sql`),
применяется той же командой `psql "$DATABASE_URL" -f migrations/00N_*.sql`
и локально, и на Supabase. `001_init.sql` не правим.
