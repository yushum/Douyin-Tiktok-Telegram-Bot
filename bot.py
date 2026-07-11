import os
import re
import io
import html
import httpx
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, URLInputFile, FSInputFile, BufferedInputFile
from aiogram.filters import CommandStart, Command, Filter
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramEntityTooLarge, TelegramBadRequest
from aiogram.utils.media_group import MediaGroupBuilder
from parsehub import ParseHub
from parsehub.types.media_ref import VideoRef, ImageRef, LivePhotoRef, AniRef
from collections.abc import Sequence

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_BASE_URL = os.environ.get("API_BASE_URL", "https://douyin.wtf")
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER")
ALLOWED_CHAT_IDS_STR = os.environ.get("ALLOWED_CHAT_IDS", "")
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", None)

ALLOWED_CHAT_IDS = []
if ALLOWED_CHAT_IDS_STR:
    for x in ALLOWED_CHAT_IDS_STR.split(","):
        x = x.strip()
        if x:
            try:
                ALLOWED_CHAT_IDS.append(int(x))
            except ValueError:
                pass

if LOCAL_API_SERVER:
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(LOCAL_API_SERVER, is_local=True)
    )
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp = Dispatcher()
URL_REGEX = re.compile(r"https?://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")

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

class WhiteListFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        if not ALLOWED_CHAT_IDS:
            return True
        if message.chat.id in ALLOWED_CHAT_IDS:
            return True
        if message.from_user and message.from_user.id in ALLOWED_CHAT_IDS:
            return True
        return False

async def process_with_parsehub_fallback(target_url: str, message: Message, reply_msg: Message = None, reply_to_msg_id: int = None):
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
                    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=60.0) as media_client:
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
                                    reply_to_message_id=reply_to_msg_id if i == 0 else None,
                                    request_timeout=60
                                )
                except Exception as sub_e:
                    print(f"MediaGroup send error (ParseHub): {sub_e}")
                
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
                    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0) as cover_client:
                        cover_resp = await cover_client.get(v_ref.thumb_url)
                        if cover_resp.status_code == 200:
                            thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
                except:
                    pass
                    
            try:
                async with httpx.AsyncClient(timeout=10.0) as head_client:
                    head_r = await head_client.head(video_url, headers={"User-Agent": "Mozilla/5.0"})
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
                try:
                    temp_filename = f"video_ph_{message.message_id}.mp4"
                    temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                    
                    async with httpx.AsyncClient(timeout=600.0) as dl_client:
                        async with dl_client.stream("GET", video_url) as video_response:
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
                        reply_to_message_id=reply_to_msg_id,
                        supports_streaming=True,
                        request_timeout=600
                    )
                    if reply_msg: await reply_msg.delete()
                    elif not reply_msg: await message.delete()
                    
                    if os.path.exists(temp_filepath):
                        os.remove(temp_filepath)
                        
                except Exception as sub_e:
                    print(f"Fallback download error (ParseHub): {str(sub_e)}")
                    if reply_msg: await reply_msg.edit_text("解析失败: 视频过大或下载超时 (ParseHub)")
                    if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                        os.remove(temp_filepath)
                        
    except Exception as ph_e:
        print(f"ParseHub Error: {str(ph_e)}")
        if reply_msg: await reply_msg.edit_text("所有解析方式均失败")

@dp.message(CommandStart(), WhiteListFilter())
async def cmd_start(message: Message):
    await message.reply("发送带有抖音/TikTok分享链接的消息给我，我会为你提取无水印视频或图集。")

@dp.message(Command("help"), WhiteListFilter())
async def cmd_help(message: Message):
    await message.reply("直接向我发送包含抖音或TikTok分享链接的消息即可，支持视频和图集解析。")

@dp.message(F.text, WhiteListFilter())
async def handle_message(message: Message):
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
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=60.0) as media_client:
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
                            print(f"MediaGroup send error: {sub_e}")
                        
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
                        cover_resp = await client.get(cover_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=10.0)
                        if cover_resp.status_code == 200:
                            thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
                    except Exception as e:
                        print(f"Cover download skipped: {e}")
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
                            print(f"Fallback download error: {str(sub_e)}")
                            await reply_msg.edit_text("解析失败: 视频过大或下载超时")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                else:
                    await process_with_parsehub_fallback(target_url, message, reply_msg, message.message_id)
            else:
                await process_with_parsehub_fallback(target_url, message, reply_msg, message.message_id)

        except Exception as e:
            print(f"Error occurred: {str(e)}")
            await process_with_parsehub_fallback(target_url, message, reply_msg, message.message_id)

@dp.channel_post(F.text, WhiteListFilter())
async def handle_channel_post(message: Message):
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
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=60.0) as media_client:
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
                            print(f"Channel MediaGroup send error: {sub_e}")
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
                        cover_resp = await client.get(cover_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=10.0)
                        if cover_resp.status_code == 200:
                            thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
                    except Exception as e:
                        print(f"Cover download skipped: {e}")
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
                            print(f"Channel Fallback download error: {str(sub_e)}")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)

                else:
                    await process_with_parsehub_fallback(target_url, message, None, None)
            else:
                await process_with_parsehub_fallback(target_url, message, None, None)

        except Exception as e:
            print(f"Channel Error occurred: {str(e)}")
            await process_with_parsehub_fallback(target_url, message, None, None)

async def main():
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())