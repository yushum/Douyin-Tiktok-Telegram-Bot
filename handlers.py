import re
from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.filters import CommandStart, Command, Filter

from config import ALLOWED_CHAT_IDS, logger
from crawler_service import parse_url_multi_engine
from utils import get_shared_client, send_parsed_media

router = Router()
URL_REGEX = re.compile(r"https?://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")
SUPPORTED_DOMAINS = ["douyin", "tiktok", "snssdk", "iesdouyin", "b23.tv", "bilibili"]


class WhiteListFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        if not ALLOWED_CHAT_IDS:
            return True
        if message.chat.id in ALLOWED_CHAT_IDS:
            return True
        if message.from_user and message.from_user.id in ALLOWED_CHAT_IDS:
            return True
        logger.info(
            f"白名单拦截: Chat_ID={message.chat.id}, "
            f"User_ID={message.from_user.id if message.from_user else 'Unknown'}"
        )
        return False


@router.message(CommandStart(), WhiteListFilter())
async def cmd_start(message: Message):
    await message.reply(
        "👋 你好！发送带有 抖音 / TikTok 分享链接的消息给我，我会为你提取无水印超清视频或图集。"
    )


@router.message(Command("help"), WhiteListFilter())
async def cmd_help(message: Message):
    await message.reply(
        "💡 直接向我发送包含抖音或 TikTok 分享链接的消息即可。\n"
        "✨ 支持：无水印超清视频、无损图集、实况 LivePhoto、最大 2GB 大文件直传。"
    )


@router.message(F.text, WhiteListFilter())
async def handle_message(message: Message, bot: Bot):
    # Extract and deduplicate URLs preserving order
    raw_urls = URL_REGEX.findall(message.text)
    urls = list(dict.fromkeys(raw_urls))
    if not urls:
        return

    # In groups/supergroups, only respond when relevant domains are present
    if message.chat.type in ["group", "supergroup"]:
        if not any(domain in url.lower() for url in urls for domain in SUPPORTED_DOMAINS):
            return

    reply_msg = await message.reply(f"🔍 已识别到 {len(urls)} 个链接，正在处理中...")
    client = get_shared_client()

    for target_url in urls:
        try:
            result = await parse_url_multi_engine(target_url, client)
            if result:
                sent = await send_parsed_media(
                    bot=bot,
                    chat_id=message.chat.id,
                    result=result,
                    reply_to_msg_id=message.message_id
                )
                if not sent:
                    await message.reply(f"❌ 发送失败: 无法将媒体发送至 Telegram ({target_url})")
            else:
                await message.reply(f"❌ 解析失败: 未能获取到媒体资源 ({target_url})")
        except Exception as e:
            logger.error(f"处理链接异常 ({target_url}): {e}", exc_info=True)
            await message.reply(f"❌ 处理异常: {e}")

    # Delete processing status message
    if reply_msg:
        try:
            await reply_msg.delete()
        except Exception:
            pass


@router.channel_post(F.text, WhiteListFilter())
async def handle_channel_post(message: Message, bot: Bot):
    raw_urls = URL_REGEX.findall(message.text)
    urls = list(dict.fromkeys(raw_urls))
    if not urls:
        return

    if not any(domain in url.lower() for url in urls for domain in SUPPORTED_DOMAINS):
        return

    client = get_shared_client()
    success_count = 0

    for target_url in urls:
        try:
            result = await parse_url_multi_engine(target_url, client)
            if result:
                sent = await send_parsed_media(
                    bot=bot,
                    chat_id=message.chat.id,
                    result=result,
                    reply_to_msg_id=None
                )
                if sent:
                    success_count += 1
        except Exception as e:
            logger.error(f"频道处理链接异常 ({target_url}): {e}", exc_info=True)

    # If successfully parsed and posted to channel, delete the original text post
    if success_count > 0:
        try:
            await message.delete()
        except Exception:
            pass


@router.message()
async def debug_catch_all(message: Message):
    logger.debug(
        f"未处理消息: Chat_ID={message.chat.id}, "
        f"User_ID={message.from_user.id if message.from_user else 'Unknown'}, "
        f"Type={message.content_type}"
    )
