import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from src.config import settings
from src.db import db
from src import handlers, admin
from src.background import background_loop
from src.health_server import start_health_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=settings.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(admin.router)
dp.include_router(handlers.router)


async def main() -> None:
    await db.connect()
    runner = await start_health_server()
    background = asyncio.create_task(background_loop(bot))
    try:
        logger.info("🚀 Starting polling...")
        await dp.start_polling(bot)
    finally:
        background.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
