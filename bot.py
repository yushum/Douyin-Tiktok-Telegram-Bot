import asyncio
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from config import BOT_TOKEN, LOCAL_API_SERVER, LOCAL_API_STORAGE_ENABLED, logger
from handlers import router
from utils import close_shared_client, cleanup_temp_dir

async def setup_bot_commands(bot: Bot):
    """Register command suggestions menu in Telegram UI."""
    commands = [
        BotCommand(command="start", description="开始使用 / 欢迎信息"),
        BotCommand(command="help", description="使用说明与支持格式"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Telegram 快捷指令菜单注册成功。")
    except Exception as e:
        logger.warning(f"注册 Telegram 快捷指令菜单失败: {e}")

async def main():
    # 启动时清理历史残留临时文件
    cleanup_temp_dir()

    if LOCAL_API_SERVER:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(
                LOCAL_API_SERVER,
                is_local=LOCAL_API_STORAGE_ENABLED
            )
        )
        bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    else:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher()
    dp.include_router(router)
    dp.shutdown.register(on_shutdown)
    
    await setup_bot_commands(bot)

    logger.info("Bot is running...")
    await dp.start_polling(bot)

async def on_shutdown(**kwargs):
    """Gracefully close shared resources on shutdown."""
    logger.info("Shutting down, closing HTTP client...")
    await close_shared_client()

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop 高性能事件循环已启用。")
    except (ImportError, AttributeError):
        logger.info("未启用 uvloop，使用标准 asyncio 事件循环。")

    asyncio.run(main())
