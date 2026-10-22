"""
Keep-alive сервер для free Render Web Service. Подключён к main.py: free
сервис засыпает после 15 минут без входящего HTTP, а фоновая задача (закрытие
брошенных сессий) должна жить, поэтому внешний пингер (UptimeRobot /
cron-job.org) бьёт в /health каждые 10–14 минут.

/health отвечает на GET и HEAD и НЕ трогает БД: пинг каждые 10 минут иначе
создавал бы лишнюю нагрузку на БД ради пустой проверки. Глубокая проверка с БД —
/health/db (для ручной диагностики, не для пингера).
"""

import logging

from aiohttp import web

from src.config import settings
from src.db import db

logger = logging.getLogger(__name__)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "daily-ten"})


async def health_db(request: web.Request) -> web.Response:
    ok = await db.ping()
    return web.json_response({"status": "ok" if ok else "degraded"}, status=200 if ok else 503)


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    # add_get регистрирует и HEAD (allow_head=True по умолчанию).
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ping", health)
    app.router.add_get("/health/db", health_db)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.PORT)
    await site.start()
    logger.info(f"🌐 Health server on :{settings.PORT}")
    return runner
