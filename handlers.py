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
MAX_URLS_PER_MSG = 5  # 防 DoS: 单条消息最大处理链接数限制


class WhiteListFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        if not ALLOWED_CHAT_IDS:
            return True

        chat_type = message.chat.type
        # 私聊场景：校验发送用户 ID 或 私聊 Chat ID
        if chat_type == "private":
            user_id = message.from_user.id if message.from_user else message.chat.id
            if user_id in ALLOWED_CHAT_IDS or message.chat.id in ALLOWED_CHAT_IDS:
                return True
        # 群组/超级群场景：群组自身必须在白名单内方可响应
        elif chat_type in ("group", "supergroup"):
            if message.chat.id in ALLOWED_CHAT_IDS:
                return True
        # 频道场景：频道自身必须在白名单内方可响应
        elif chat_type == "channel":
            if message.chat.id in ALLOWED_CHAT_IDS:
                return True

        logger.info(
            f"白名单拦截: Chat_Type={chat_type}, Chat_ID={message.chat.id}, "
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

    # 防 DoS 与无关链接过滤：只保留受支持平台的 URL
    valid_urls = [u for u in urls if any(domain in u.lower() for domain in SUPPORTED_DOMAINS)]

    if not valid_urls:
        if message.chat.type == "private":
            await message.reply("👋 未识别到支持的平台链接，请发送包含 抖音 或 TikTok 的分享链接。")
        return

    # 截断限制单次最大处理链接数量
    if len(valid_urls) > MAX_URLS_PER_MSG:
        valid_urls = valid_urls[:MAX_URLS_PER_MSG]

    reply_msg = await message.reply(f"🔍 已识别到 {len(valid_urls)} 个链接，正在处理中...", parse_mode=None)
    client = get_shared_client()

    for target_url in valid_urls:
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
                    await message.reply(f"❌ 发送失败: 无法将媒体发送至 Telegram ({target_url})", parse_mode=None)
            else:
                await message.reply(f"❌ 解析失败: 未能获取到媒体资源 ({target_url})", parse_mode=None)
        except Exception as e:
            logger.error(f"处理链接异常 ({target_url}): {e}", exc_info=True)
            await message.reply("❌ 处理异常，请稍后重试或检查链接是否有效。", parse_mode=None)

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

    valid_urls = [u for u in urls if any(domain in u.lower() for domain in SUPPORTED_DOMAINS)]
    if not valid_urls:
        return

    if len(valid_urls) > MAX_URLS_PER_MSG:
        valid_urls = valid_urls[:MAX_URLS_PER_MSG]

    client = get_shared_client()
    success_count = 0

    for target_url in valid_urls:
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
