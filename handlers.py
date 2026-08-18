import re
import uuid
import asyncio
from typing import Union
from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    InlineQuery,
    InlineQueryResultVideo,
    InlineQueryResultPhoto,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult,
)
from aiogram.filters import CommandStart, Command, Filter

from config import ALLOWED_CHAT_IDS, logger
from crawler_service import parse_url_multi_engine
from utils import get_shared_client, send_parsed_media, build_caption

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
        "👋 你好！发送带有 抖音 / TikTok 分享链接的消息给我，我会为你提取无水印超清视频或图集。\n\n"
        "💡 <b>行内模式</b>：在任意聊天框中输入 <code>@本Bot用户名 分享文本或链接</code> 即可直接选单发送视频！"
    )


@router.message(Command("help"), WhiteListFilter())
async def cmd_help(message: Message):
    await message.reply(
        "💡 <b>使用说明</b>：\n"
        "1. 直接向我发送包含抖音或 TikTok 分享链接的消息；\n"
        "2. 在任意群聊/私聊中输入 <code>@Bot用户名 分享文本</code> 唤起行内选单直接发送。\n\n"
        "✨ <b>支持特性</b>：无水印超清视频、无损图集、实况 LivePhoto、最大 2GB 大文件直传。"
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


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery, bot: Bot):
    """Handle Telegram Inline Query (@bot <share_text_or_url>)."""
    # 1. 校验白名单权限
    if ALLOWED_CHAT_IDS:
        user_id = inline_query.from_user.id if inline_query.from_user else None
        if not user_id or user_id not in ALLOWED_CHAT_IDS:
            logger.info(f"白名单拦截 InlineQuery: User_ID={user_id}")
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id="unauthorized",
                        title="⛔ 无使用权限",
                        description="您的 Telegram 账号未在白名单中，无法使用行内解析功能。",
                        input_message_content=InputTextMessageContent(
                            message_text="⛔ 抱歉，您没有使用此 Bot 行内解析功能的权限。"
                        )
                    )
                ],
                cache_time=10,
                is_personal=True
            )
            return

    query_text = (inline_query.query or "").strip()

    # 2. 空输入提示
    if not query_text:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="hint",
                    title="💡 粘贴抖音或 TikTok 分享链接",
                    description="支持视频、图集、LivePhoto 实况，附带其他文本均可自动识别",
                    input_message_content=InputTextMessageContent(
                        message_text="💡 请在 @Bot 后附带抖音或 TikTok 的分享链接以直接发送无水印媒体。"
                    )
                )
            ],
            cache_time=30,
            is_personal=True
        )
        return

    # 3. 提取并过滤有效 URL
    raw_urls = URL_REGEX.findall(query_text)
    valid_urls = [u for u in raw_urls if any(domain in u.lower() for domain in SUPPORTED_DOMAINS)]

    if not valid_urls:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="no_valid_url",
                    title="🔍 未识别到受支持的平台链接",
                    description="请确保内容中包含 抖音(v.douyin.com) 或 TikTok(tiktok.com) 链接",
                    input_message_content=InputTextMessageContent(
                        message_text="⚠️ 未识别到受支持的抖音或 TikTok 分享链接。"
                    )
                )
            ],
            cache_time=10,
            is_personal=True
        )
        return

    target_url = valid_urls[0]
    client = get_shared_client()

    try:
        # 设置 10 秒超时，避免客户端等待过久
        result = await asyncio.wait_for(
            parse_url_multi_engine(target_url, client),
            timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.warning(f"InlineQuery 解析超时 ({target_url})")
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="timeout",
                    title="⏱️ 解析超时",
                    description="解析耗时过长，建议直接在与 Bot 的私聊或群聊中发送该链接",
                    input_message_content=InputTextMessageContent(
                        message_text=f"⏱️ 链接解析超时 ({target_url})，请直接向机器人发送该链接进行解析。"
                    )
                )
            ],
            cache_time=5,
            is_personal=True
        )
        return
    except Exception as e:
        logger.error(f"InlineQuery 解析异常 ({target_url}): {e}", exc_info=True)
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="error",
                    title="❌ 解析异常",
                    description="处理出现异常，请稍后重试",
                    input_message_content=InputTextMessageContent(
                        message_text=f"❌ 解析异常，请稍后重试 ({target_url})"
                    )
                )
            ],
            cache_time=5,
            is_personal=True
        )
        return

    if not result:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="not_found",
                    title="❌ 解析失败",
                    description="未能获取到该链接的媒体资源，可能已被删除或设为私密",
                    input_message_content=InputTextMessageContent(
                        message_text=f"❌ 未能解析到媒体资源 ({target_url})"
                    )
                )
            ],
            cache_time=10,
            is_personal=True
        )
        return

    caption = build_caption(result.title, result.canonical_url)
    results = []
    base_id = result.aweme_id or uuid.uuid4().hex[:8]

    if result.media_type == "video" and result.video_url:
        cover_thumb = result.cover_url or "https://p16-sign-va.tiktokcdn.com/tos-useast2a-avt-0068-giso/cover.jpeg"
        video_item = InlineQueryResultVideo(
            id=f"vid_{base_id}",
            video_url=result.video_url,
            mime_type="video/mp4",
            thumbnail_url=cover_thumb,
            title=result.title[:64] if result.title else "无水印超清视频",
            caption=caption,
            parse_mode="HTML",
            description="🎬 点击直接在当前聊天发送无水印视频",
            video_width=result.video_width,
            video_height=result.video_height,
        )
        results.append(video_item)

    elif result.media_type == "image" and result.media_assets:
        # Telegram Inline 限制单条结果发送单项媒体，展示图集中的图片供用户选择
        for idx, asset in enumerate(result.media_assets[:20]):
            item_caption = caption if idx == 0 else f"{caption}\n(图 {idx + 1}/{len(result.media_assets)})"
            if asset.type == "video":
                results.append(
                    InlineQueryResultVideo(
                        id=f"img_v_{base_id}_{idx}",
                        video_url=asset.url,
                        mime_type="video/mp4",
                        thumbnail_url=result.cover_url or asset.url,
                        title=f"实况 LivePhoto ({idx + 1}/{len(result.media_assets)})",
                        caption=item_caption,
                        parse_mode="HTML",
                        description=f"点击发送第 {idx + 1} 个实况动态视频",
                        video_width=asset.width,
                        video_height=asset.height,
                    )
                )
            else:
                results.append(
                    InlineQueryResultPhoto(
                        id=f"img_p_{base_id}_{idx}",
                        photo_url=asset.url,
                        thumbnail_url=asset.url,
                        title=f"图集 ({idx + 1}/{len(result.media_assets)})",
                        caption=item_caption,
                        parse_mode="HTML",
                        description=f"点击发送第 {idx + 1}/{len(result.media_assets)} 张无损图片",
                        photo_width=asset.width,
                        photo_height=asset.height,
                    )
                )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id="no_media",
                title="⚠️ 未找到可发送的媒体",
                description="解析成功但未提取到有效的视频或图片直链",
                input_message_content=InputTextMessageContent(
                    message_text=f"⚠️ 未找到可发送的媒体 ({target_url})"
                )
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=300,
        is_personal=True
    )


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen_result: ChosenInlineResult):
    logger.debug(
        f"用户选中行内结果: User_ID={chosen_result.from_user.id}, "
        f"Result_ID={chosen_result.result_id}, Query={chosen_result.query}"
    )


@router.message()
async def debug_catch_all(message: Message):
    logger.debug(
        f"未处理消息: Chat_ID={message.chat.id}, "
        f"User_ID={message.from_user.id if message.from_user else 'Unknown'}, "
        f"Type={message.content_type}"
    )
