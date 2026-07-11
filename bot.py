import asyncio
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from config import BOT_TOKEN, LOCAL_API_SERVER, logger
from handlers import router

async def main():
    if LOCAL_API_SERVER:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(LOCAL_API_SERVER, is_local=True)
        )
        bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    else:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher()
    dp.include_router(router)
    
    logger.info("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())