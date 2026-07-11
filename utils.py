import os
import html
import httpx
import asyncio
import uuid
import aiofiles
from aiogram import Bot
from aiogram.types import Message, URLInputFile, FSInputFile, BufferedInputFile
from aiogram.exceptions import TelegramEntityTooLarge, TelegramBadRequest
from aiogram.utils.media_group import MediaGroupBuilder
from parsehub import ParseHub
from parsehub.types.media_ref import VideoRef, ImageRef, LivePhotoRef, AniRef
from collections.abc import Sequence

from config import DOUYIN_COOKIE, logger

_shared_client = None

def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=60.0)
    return _shared_client

def get_best_video_url(video_info, root_data=None):
    video_url = None
    max_width = 0
    max_bitrate = 0
    
    bit_rate_list = video_info.get("bit_rate", [])
    for rate in bit_rate_list:
        play_addr = rate.get("play_addr", {})
        current_width = play_addr.get("width", 0)
        current_bitrate = rate.get("bit_rate", 0)
        url_list = play_addr.get("url_list", [])
        
        if not url_list:
            continue

        if current_width > max_width or (current_width == max_width and current_bitrate > max_bitrate):
            max_width = current_width
            max_bitrate = current_bitrate
            video_url = url_list[0]
            
    if not video_url:
        play_addr = video_info.get("play_addr", {})
        url_list = play_addr.get("url_list", [])
        if url_list:
            video_url = url_list[0]
            
    if not video_url and root_data:
        video_dict = root_data.get("video_data", {})
        video_url = video_dict.get("nwm_video_url_HQ") or video_dict.get("nwm_video_url")
        
    if video_url and "/playwm/" in video_url:
        video_url = video_url.replace("/playwm/", "/play/")
        
    return video_url

async def process_with_parsehub_fallback(target_url: str, message: Message, bot: Bot, reply_msg: Message = None, reply_to_msg_id: int = None):
    try:
        ph = ParseHub()
        result = await ph.parse(target_url, cookie=DOUYIN_COOKIE)
        
        video_link = getattr(result, "url", target_url) or target_url
        safe_desc = html.escape(result.title or "")
        caption = f"<a href='{video_link}'>{safe_desc}</a>" if safe_desc else f"<a href='{video_link}'>视频链接</a>"
        
        media_list = []
        if isinstance(result.media, Sequence):
            media_list = list(result.media)
        elif result.media:
            media_list = [result.media]
            
        if not media_list:
            if reply_msg: await reply_msg.edit_text("解析失败: 未找到有效媒体源 (ParseHub)")
            return
            
        is_video = isinstance(media_list[0], (VideoRef, AniRef))
        
        if not is_video:
            media_assets = []
            for m in media_list:
                if isinstance(m, LivePhotoRef) and m.video_url:
                    media_assets.append({"type": "video", "url": m.video_url, "width": m.width, "height": m.height})
                else:
                    media_assets.append({"type": "photo", "url": m.url, "width": m.width, "height": m.height})
                    
            if media_assets:
                try:
                    media_client = get_shared_client()
                    for i in range(0, len(media_assets), 10):
                        chunk = media_assets[i:i+10]
                        media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
                        
                        download_tasks = [media_client.get(asset["url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) for asset in chunk]
                        responses = await asyncio.gather(*download_tasks, return_exceptions=True)
                            
                            buffer_list = []
                            for idx, res in enumerate(responses):
                                if isinstance(res, httpx.Response) and res.status_code == 200:
                                    asset = chunk[idx]
                                    asset_type = asset["type"]
                                    ext = "webp" if asset_type == "photo" else "mp4"
                                    buffer_list.append({
                                        "bytes": res.content,
                                        "filename": f"media_{i}_{idx}.{ext}",
                                        "type": asset_type,
                                        "width": asset.get("width"),
                                        "height": asset.get("height")
                                    })
                                    
                            for item in buffer_list:
                                file_obj = BufferedInputFile(item["bytes"], filename=item["filename"])
                                if item["type"] == "photo":
                                    media_group.add_photo(media=file_obj)
                                else:
                                    kwargs = {}
                                    if item.get("width"): kwargs["width"] = int(item["width"])
                                    if item.get("height"): kwargs["height"] = int(item["height"])
                                    media_group.add_video(media=file_obj, **kwargs)
                            
                            if buffer_list:
                                await bot.send_media_group(
                                    chat_id=message.chat.id,
                                    media=media_group.build(),
                                    reply_to_message_id=reply_to_msg_id if i == 0 else None,
                                    request_timeout=60
                                )
                except Exception as sub_e:
                    logger.error(f"MediaGroup send error (ParseHub): {sub_e}")
                
                if reply_msg: await reply_msg.delete()
                elif not reply_msg: await message.delete()
                
        else:
            v_ref = media_list[0]
            video_url = v_ref.url
            vid_width = v_ref.width
            vid_height = v_ref.height
            
            thumbnail_file = None
            if getattr(v_ref, 'thumb_url', None):
                try:
                    cover_client = get_shared_client()
                    cover_resp = await cover_client.get(v_ref.thumb_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
                    if cover_resp.status_code == 200:
                        thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
                except:
                    pass
                    
            try:
                head_client = get_shared_client()
                head_r = await head_client.head(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
                content_length = int(head_r.headers.get("content-length", 0))
            except:
                content_length = 0
                
            try:
                if content_length > 18 * 1024 * 1024:
                    raise TelegramEntityTooLarge("File size likely exceeds Telegram URL upload limit")
                    
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
                    request_timeout=120
                )
                if reply_msg: await reply_msg.delete()
                elif not reply_msg: await message.delete()
                
            except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError, Exception) as e:
                temp_filename = f"video_ph_{message.chat.id}_{message.message_id}_{uuid.uuid4().hex[:6]}.mp4"
                temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                
                try:
                    dl_client = get_shared_client()
                    async with dl_client.stream("GET", video_url, timeout=600.0) as video_response:
                        video_response.raise_for_status()
                        async with aiofiles.open(temp_filepath, "wb") as f:
                            async for chunk in video_response.aiter_bytes(chunk_size=1024*1024):
                                await f.write(chunk)
                    
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
                        request_timeout=600
                    )
                    if reply_msg: await reply_msg.delete()
                    elif not reply_msg: await message.delete()
                        
                except Exception as sub_e:
                    logger.error(f"Fallback download error (ParseHub)", exc_info=True)
                    if reply_msg: await reply_msg.edit_text("解析失败: 视频过大或下载超时 (ParseHub)")
                finally:
                    if os.path.exists(temp_filepath):
                        os.remove(temp_filepath)
                        
    except Exception as ph_e:
        logger.error(f"ParseHub Error", exc_info=True)
        if reply_msg: await reply_msg.edit_text("所有解析方式均失败")
