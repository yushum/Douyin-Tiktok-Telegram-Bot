import os
import re
import html
import uuid
import httpx
import asyncio
import aiofiles
from aiogram import Router, F, Bot
from aiogram.types import Message, URLInputFile, FSInputFile, BufferedInputFile
from aiogram.filters import CommandStart, Command, Filter
from aiogram.exceptions import TelegramEntityTooLarge, TelegramBadRequest
from aiogram.utils.media_group import MediaGroupBuilder

from config import API_BASE_URL, ALLOWED_CHAT_IDS, TEMP_DIR, logger
from utils import get_best_video_url, process_with_parsehub_fallback, get_shared_client, FileTooLargeError

router = Router()
URL_REGEX = re.compile(r"https?://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")
SUPPORTED_DOMAINS = ["douyin", "tiktok", "snssdk"]


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


async def _download_and_build_media_group(client, chunk, caption, is_first_chunk):
    """Download media assets concurrently and build a MediaGroup.
    
    Returns:
        tuple: (media_group, buffer_list) — buffer_list may be empty if all downloads failed.
    """
    media_group = MediaGroupBuilder(caption=caption if is_first_chunk else None)
    
    download_tasks = [
        client.get(asset["url"], headers={"User-Agent": "Mozilla/5.0"})
        for asset in chunk
    ]
    responses = await asyncio.gather(*download_tasks, return_exceptions=True)
    
    buffer_list = []
    for idx, res in enumerate(responses):
        if isinstance(res, httpx.Response) and res.status_code == 200:
            asset = chunk[idx]
            asset_type = asset["type"]
            ext = "webp" if asset_type == "photo" else "mp4"
            buffer_list.append({
                "bytes": res.content,
                "filename": f"media_{idx}.{ext}",
                "type": asset_type,
                "width": asset.get("width"),
                "height": asset.get("height"),
            })
    
    for item in buffer_list:
        file_obj = BufferedInputFile(item["bytes"], filename=item["filename"])
        if item["type"] == "photo":
            media_group.add_photo(media=file_obj)
        else:
            kwargs = {}
            if item.get("width"):
                kwargs["width"] = int(item["width"])
            if item.get("height"):
                kwargs["height"] = int(item["height"])
            media_group.add_video(media=file_obj, **kwargs)
    
    return media_group, buffer_list


async def _process_single_url(
    target_url: str,
    message: Message,
    bot: Bot,
    client: httpx.AsyncClient,
    api_endpoint: str,
    reply_msg: Message = None,
    reply_to_msg_id: int = None,
    send_error_reply: bool = True,
):
    """Process a single URL: call API, handle images/video, or fall back to ParseHub.
    
    Args:
        target_url: The URL to process.
        message: The original message.
        bot: The bot instance.
        client: The httpx client.
        api_endpoint: The API endpoint URL.
        reply_msg: The "processing..." reply (None for channel posts).
        reply_to_msg_id: Message ID to reply to (None for channel posts).
        send_error_reply: Whether to send error replies to the user on failure.
    """
    try:
        response = await client.get(
            api_endpoint,
            params={"url": target_url, "minimal": "false"},
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 200:
            await process_with_parsehub_fallback(target_url, message, bot, reply_msg, reply_to_msg_id)
            return

        root_data = data.get("data") or {}
        aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data

        raw_desc = aweme_detail.get("desc", "")
        safe_desc = html.escape(raw_desc)

        # Build video link
        aweme_id = aweme_detail.get("aweme_id")
        if aweme_id:
            if "tiktok" in target_url.lower():
                author_info = aweme_detail.get("author") or {}
                unique_id = author_info.get("unique_id") or author_info.get("short_id") or "user"
                video_link = f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}"
            else:
                video_link = f"https://www.douyin.com/video/{aweme_id}"
        else:
            video_link = target_url

        caption = f"<a href='{video_link}'>{safe_desc}</a>" if safe_desc else f"<a href='{video_link}'>视频链接</a>"

        # === Handle images ===
        images = aweme_detail.get("images") or []
        if images:
            media_assets = []
            for img in images:
                live_video = img.get("video") or {}
                live_url = None
                if live_video:
                    live_url = get_best_video_url(live_video)

                if live_url:
                    v_width = live_video.get("play_addr", {}).get("width") or live_video.get("width")
                    v_height = live_video.get("play_addr", {}).get("height") or live_video.get("height")
                    media_assets.append({
                        "type": "video",
                        "url": live_url,
                        "width": v_width,
                        "height": v_height,
                    })
                else:
                    url_list = img.get("url_list") or []
                    img_url = url_list[0] if url_list else None
                    if img_url:
                        media_assets.append({"type": "photo", "url": img_url})

            if media_assets:
                try:
                    for i in range(0, len(media_assets), 10):
                        chunk = media_assets[i:i + 10]
                        media_group, buffer_list = await _download_and_build_media_group(
                            client, chunk, caption, is_first_chunk=(i == 0),
                        )

                        if buffer_list:
                            await bot.send_media_group(
                                chat_id=message.chat.id,
                                media=media_group.build(),
                                reply_to_message_id=reply_to_msg_id if i == 0 else None,
                                request_timeout=60,
                            )
                except Exception:
                    logger.error("MediaGroup send error", exc_info=True)
                return  # Images handled, done with this URL

        # === Handle video ===
        video_info = aweme_detail.get("video") or {}

        play_addr_info = video_info.get("play_addr") or {}
        vid_width = play_addr_info.get("width") or video_info.get("width")
        vid_height = play_addr_info.get("height") or video_info.get("height")
        if vid_width:
            vid_width = int(vid_width)
        if vid_height:
            vid_height = int(vid_height)

        # Download thumbnail
        cover_url = None
        cover_dict = video_info.get("origin_cover") or video_info.get("cover") or {}
        if cover_dict.get("url_list"):
            cover_url = cover_dict["url_list"][0]

        thumbnail_file = None
        if cover_url:
            try:
                cover_resp = await client.get(cover_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
                if cover_resp.status_code == 200:
                    thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
            except Exception as e:
                logger.warning(f"Cover download skipped: {e}")

        video_url = get_best_video_url(video_info, root_data)

        if video_url:
            # Check file size via HEAD request
            try:
                head_r = await client.head(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
                content_length = int(head_r.headers.get("content-length", 0))
            except Exception:
                content_length = 0

            try:
                if content_length > 18 * 1024 * 1024:
                    raise FileTooLargeError(f"File size {content_length} exceeds limit")

                video_file = URLInputFile(video_url)
                await bot.send_video(
                    chat_id=message.chat.id,
                    video=video_file,
                    caption=caption,
                    thumbnail=thumbnail_file,
                    width=vid_width,
                    height=vid_height,
                    reply_to_message_id=reply_to_msg_id,
                    supports_streaming=True,
                    request_timeout=120,
                )
            except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError, FileTooLargeError):
                # Fallback: download to local file and re-upload
                logger.info(f"URL upload failed, falling back to local download for {target_url}")
                temp_filename = f"video_{message.chat.id}_{message.message_id}_{uuid.uuid4().hex[:6]}.mp4"
                temp_filepath = os.path.join(TEMP_DIR, temp_filename)
                try:
                    async with client.stream("GET", video_url, timeout=600.0) as video_response:
                        video_response.raise_for_status()
                        async with aiofiles.open(temp_filepath, "wb") as f:
                            async for data_chunk in video_response.aiter_bytes(chunk_size=1024 * 1024):
                                await f.write(data_chunk)

                    local_video_file = FSInputFile(temp_filepath)
                    await bot.send_video(
                        chat_id=message.chat.id,
                        video=local_video_file,
                        caption=caption,
                        thumbnail=thumbnail_file,
                        width=vid_width,
                        height=vid_height,
                        reply_to_message_id=reply_to_msg_id,
                        supports_streaming=True,
                        request_timeout=600,
                    )
                except Exception:
                    logger.error("Fallback download error", exc_info=True)
                    if send_error_reply:
                        await message.reply(f"解析失败: 视频过大或下载超时 ({target_url})")
                finally:
                    if os.path.exists(temp_filepath):
                        os.remove(temp_filepath)
        else:
            await process_with_parsehub_fallback(target_url, message, bot, reply_msg, reply_to_msg_id)

    except httpx.HTTPStatusError:
        logger.error(f"API HTTP error for {target_url}", exc_info=True)
        await process_with_parsehub_fallback(target_url, message, bot, reply_msg, reply_to_msg_id)
    except Exception:
        logger.error(f"Error processing {target_url}", exc_info=True)
        await process_with_parsehub_fallback(target_url, message, bot, reply_msg, reply_to_msg_id)


@router.message(CommandStart(), WhiteListFilter())
async def cmd_start(message: Message):
    await message.reply("发送带有抖音/TikTok分享链接的消息给我，我会为你提取无水印视频或图集。")

@router.message(Command("help"), WhiteListFilter())
async def cmd_help(message: Message):
    await message.reply("直接向我发送包含抖音或TikTok分享链接的消息即可，支持视频和图集解析。")

@router.message(F.text, WhiteListFilter())
async def handle_message(message: Message, bot: Bot):
    urls = URL_REGEX.findall(message.text)
    if not urls:
        return
        
    if message.chat.type in ["group", "supergroup"]:
        if not any(domain in url.lower() for url in urls for domain in SUPPORTED_DOMAINS):
            return

    reply_msg = await message.reply(f"已识别到 {len(urls)} 个链接，正在处理...")
    api_endpoint = f"{API_BASE_URL}/api/hybrid/video_data"
    client = get_shared_client()

    for target_url in urls:
        await _process_single_url(
            target_url, message, bot, client, api_endpoint,
            reply_msg=reply_msg,
            reply_to_msg_id=message.message_id,
            send_error_reply=True,
        )

    if reply_msg:
        try:
            await reply_msg.delete()
        except Exception:
            pass

@router.channel_post(F.text, WhiteListFilter())
async def handle_channel_post(message: Message, bot: Bot):
    urls = URL_REGEX.findall(message.text)
    if not urls:
        return
        
    if not any(domain in url.lower() for url in urls for domain in SUPPORTED_DOMAINS):
        return

    api_endpoint = f"{API_BASE_URL}/api/hybrid/video_data"
    client = get_shared_client()
    
    for target_url in urls:
        await _process_single_url(
            target_url, message, bot, client, api_endpoint,
            reply_msg=None,
            reply_to_msg_id=None,
            send_error_reply=False,
        )

    try:
        await message.delete()
    except Exception:
        pass

@router.message()
async def debug_catch_all(message: Message):
    logger.info(
        f"拦截到未处理消息 (可能因白名单或非文本): "
        f"Chat_ID={message.chat.id}, "
        f"User_ID={message.from_user.id if message.from_user else 'Unknown'}, "
        f"Type={message.content_type}"
    )
