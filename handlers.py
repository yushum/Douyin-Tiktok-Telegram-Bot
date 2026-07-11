import os
import re
import html
import httpx
import asyncio
from aiogram import Router, F, Bot
from aiogram.types import Message, URLInputFile, FSInputFile, BufferedInputFile
from aiogram.filters import CommandStart, Command, Filter
from aiogram.exceptions import TelegramEntityTooLarge, TelegramBadRequest
from aiogram.utils.media_group import MediaGroupBuilder

from config import API_BASE_URL, ALLOWED_CHAT_IDS, logger
from utils import get_best_video_url, process_with_parsehub_fallback

router = Router()
URL_REGEX = re.compile(r"https?://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")

class WhiteListFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        if not ALLOWED_CHAT_IDS:
            return True
        if message.chat.id in ALLOWED_CHAT_IDS:
            return True
        if message.from_user and message.from_user.id in ALLOWED_CHAT_IDS:
            return True
        return False

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
        if not any(domain in urls[0].lower() for domain in ["douyin", "tiktok", "snssdk"]):
            return

    target_url = urls[0]
    reply_msg = await message.reply("正在处理...")

    api_endpoint = f"{API_BASE_URL}/api/hybrid/video_data"
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.get(
                api_endpoint, 
                params={"url": target_url, "minimal": "false"}
            )
            data = response.json()

            if data.get("code") == 200:
                root_data = data.get("data", {})
                aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data
                
                # HTML Escape for desc to prevent Telegram Parse Error
                raw_desc = aweme_detail.get("desc", "")
                safe_desc = html.escape(raw_desc)
                
                aweme_id = aweme_detail.get("aweme_id")
                if aweme_id:
                    if "tiktok" in target_url.lower():
                        author_info = aweme_detail.get("author", {})
                        unique_id = author_info.get("unique_id") or author_info.get("short_id") or "user"
                        video_link = f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}"
                    else:
                        video_link = f"https://www.douyin.com/video/{aweme_id}"
                else:
                    video_link = target_url
                
                caption = f"<a href='{video_link}'>{safe_desc}</a>" if safe_desc else f"<a href='{video_link}'>视频链接</a>"

                images = aweme_detail.get("images", [])
                if images:
                    media_assets = []
                    for img in images:
                        live_video = img.get("video", {})
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
                                "height": v_height
                            })
                        else:
                            img_url = img.get("url_list", [])[0] if img.get("url_list") else None
                            if img_url:
                                media_assets.append({"type": "photo", "url": img_url})

                    if media_assets:
                        try:
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=60.0) as media_client:
                                for i in range(0, len(media_assets), 10):
                                    chunk = media_assets[i:i+10]
                                    media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
                                    
                                    download_tasks = [media_client.get(asset["url"]) for asset in chunk]
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
                                            reply_to_message_id=message.message_id if i == 0 else None,
                                            request_timeout=60
                                        )
                        except Exception as sub_e:
                            logger.error(f"MediaGroup send error: {sub_e}")
                        
                        await reply_msg.delete()
                        return

                video_info = aweme_detail.get("video", {})
                
                play_addr_info = video_info.get("play_addr", {})
                vid_width = play_addr_info.get("width") or video_info.get("width")
                vid_height = play_addr_info.get("height") or video_info.get("height")
                if vid_width: vid_width = int(vid_width)
                if vid_height: vid_height = int(vid_height)
                
                cover_url = None
                cover_dict = video_info.get("origin_cover", {}) or video_info.get("cover", {})
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
                        thumbnail_file = None

                video_url = get_best_video_url(video_info, root_data)
                     
                if video_url:
                    try:
                        head_r = await client.head(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
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
                            reply_to_message_id=message.message_id,
                            supports_streaming=True,
                            request_timeout=120
                        )
                        await reply_msg.delete()
                        
                    except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError, Exception) as e:
                        try:
                            temp_filename = f"video_{message.message_id}.mp4"
                            temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                            
                            async with client.stream("GET", video_url, timeout=600.0) as video_response:
                                video_response.raise_for_status()
                                with open(temp_filepath, "wb") as f:
                                    async for chunk in video_response.aiter_bytes(chunk_size=1024*1024):
                                        f.write(chunk)
                            
                            local_video_file = FSInputFile(temp_filepath)
                            
                            await bot.send_video(
                                chat_id=message.chat.id,
                                video=local_video_file,
                                caption=caption,
                                thumbnail=thumbnail_file,
                                width=vid_width,      
                                height=vid_height,    
                                reply_to_message_id=message.message_id,
                                supports_streaming=True,
                                request_timeout=600
                            )
                            await reply_msg.delete()
                            
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                                
                        except Exception as sub_e:
                            logger.error(f"Fallback download error: {str(sub_e)}")
                            await reply_msg.edit_text("解析失败: 视频过大或下载超时")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                else:
                    await process_with_parsehub_fallback(target_url, message, bot, reply_msg, message.message_id)
            else:
                await process_with_parsehub_fallback(target_url, message, bot, reply_msg, message.message_id)

        except Exception as e:
            logger.error(f"Error occurred: {str(e)}")
            await process_with_parsehub_fallback(target_url, message, bot, reply_msg, message.message_id)

@router.channel_post(F.text, WhiteListFilter())
async def handle_channel_post(message: Message, bot: Bot):
    urls = URL_REGEX.findall(message.text)
    if not urls:
        return
        
    if not any(domain in urls[0].lower() for domain in ["douyin", "tiktok", "snssdk"]):
        return

    target_url = urls[0]
    
    api_endpoint = f"{API_BASE_URL}/api/hybrid/video_data"
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.get(
                api_endpoint, 
                params={"url": target_url, "minimal": "false"}
            )
            data = response.json()

            if data.get("code") == 200:
                root_data = data.get("data", {})
                aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data
                
                # HTML Escape for desc to prevent Telegram Parse Error
                raw_desc = aweme_detail.get("desc", "")
                safe_desc = html.escape(raw_desc)
                
                aweme_id = aweme_detail.get("aweme_id")
                if aweme_id:
                    if "tiktok" in target_url.lower():
                        author_info = aweme_detail.get("author", {})
                        unique_id = author_info.get("unique_id") or author_info.get("short_id") or "user"
                        video_link = f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}"
                    else:
                        video_link = f"https://www.douyin.com/video/{aweme_id}"
                else:
                    video_link = target_url
                
                caption = f"<a href='{video_link}'>{safe_desc}</a>" if safe_desc else f"<a href='{video_link}'>视频链接</a>"

                images = aweme_detail.get("images", [])
                if images:
                    media_assets = []
                    for img in images:
                        live_video = img.get("video", {})
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
                                "height": v_height
                            })
                        else:
                            img_url = img.get("url_list", [])[0] if img.get("url_list") else None
                            if img_url:
                                media_assets.append({"type": "photo", "url": img_url})

                    if media_assets:
                        try:
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=60.0) as media_client:
                                for i in range(0, len(media_assets), 10):
                                    chunk = media_assets[i:i+10]
                                    media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
                                    
                                    download_tasks = [media_client.get(asset["url"]) for asset in chunk]
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
                                            request_timeout=60
                                        )
                            await message.delete()
                        except Exception as sub_e:
                            logger.error(f"Channel MediaGroup send error: {sub_e}")
                        return

                video_info = aweme_detail.get("video", {})
                
                play_addr_info = video_info.get("play_addr", {})
                vid_width = play_addr_info.get("width") or video_info.get("width")
                vid_height = play_addr_info.get("height") or video_info.get("height")
                if vid_width: vid_width = int(vid_width)
                if vid_height: vid_height = int(vid_height)
                
                cover_url = None
                cover_dict = video_info.get("origin_cover", {}) or video_info.get("cover", {})
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
                        thumbnail_file = None

                video_url = get_best_video_url(video_info, root_data)
                     
                if video_url:
                    try:
                        head_r = await client.head(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
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
                            supports_streaming=True,
                            request_timeout=120
                        )
                        await message.delete()
                        
                    except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError, Exception) as e:
                        try:
                            temp_filename = f"message_{message.message_id}.mp4"
                            temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                            
                            async with client.stream("GET", video_url, timeout=600.0) as video_response:
                                video_response.raise_for_status()
                                with open(temp_filepath, "wb") as f:
                                    async for chunk in video_response.aiter_bytes(chunk_size=1024*1024):
                                        f.write(chunk)
                            
                            local_video_file = FSInputFile(temp_filepath)
                            
                            await bot.send_video(
                                chat_id=message.chat.id,
                                video=local_video_file,
                                caption=caption,
                                thumbnail=thumbnail_file,
                                width=vid_width,      
                                height=vid_height,    
                                supports_streaming=True,
                                request_timeout=600
                            )
                            await message.delete()
                            
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                                
                        except Exception as sub_e:
                            logger.error(f"Channel Fallback download error: {str(sub_e)}")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)

                else:
                    await process_with_parsehub_fallback(target_url, message, bot, None, None)
            else:
                await process_with_parsehub_fallback(target_url, message, bot, None, None)

        except Exception as e:
            logger.error(f"Channel Error occurred: {str(e)}")
            await process_with_parsehub_fallback(target_url, message, bot, None, None)
