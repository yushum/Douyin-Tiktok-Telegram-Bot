import os
import re
import io
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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_BASE_URL = os.environ.get("API_BASE_URL", "https://douyin.wtf")
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER")
ALLOWED_CHAT_IDS_STR = os.environ.get("ALLOWED_CHAT_IDS", "")

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
    """
    通用寻源函数：双因子排序获取最高分辨率、最高码率的视频流，
    适用于主视频及图集内的实况动图(Live Photo)
    """
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
                
                # IMPORTANT FIX: 处理有些API版本直接把核心数据放在外层而没有 aweme_detail 包装的情况
                aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data
                
                author_info = aweme_detail.get("author", {})
                nickname = author_info.get("nickname", "未知作者")
                sec_uid = author_info.get("sec_uid", "")
                desc = aweme_detail.get("desc", "无描述")
                
                if sec_uid:
                    author_url = f"https://www.douyin.com/user/{sec_uid}"
                    caption = f"<a href='{author_url}'>{nickname}</a>\n{desc}"
                else:
                    caption = f"<b>{nickname}</b>\n{desc}"

                images = aweme_detail.get("images", [])
                if images:
                    media_assets = []
                    for img in images:
                        # 核心新增：探测实况动图 (Live Photo)
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
                            # 放宽时间至 60s，防实况视频过大导致超时
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

                # 调用复用的最优寻源函数获取主视频链接
                video_url = get_best_video_url(video_info, root_data)
                     
                if video_url:
                    try:
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
                        
                    except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError) as e:
                        try:
                            temp_filename = f"video_{message.message_id}.mp4"
                            temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                            
                            async with client.stream("GET", video_url, timeout=300.0) as video_response:
                                video_response.raise_for_status()
                                with open(temp_filepath, "wb") as f:
                                    async for chunk in video_response.aiter_bytes():
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
                                request_timeout=300
                            )
                            await reply_msg.delete()
                            
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                                
                        except Exception as sub_e:
                            print(f"Fallback download error: {str(sub_e)}")
                            await reply_msg.edit_text("解析失败")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                else:
                    await reply_msg.edit_text("解析失败")
            else:
                await reply_msg.edit_text("解析失败")

        except Exception as e:
            print(f"Error occurred: {str(e)}")
            await reply_msg.edit_text("解析失败")

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
                
                # IMPORTANT FIX: 处理有些API版本直接把核心数据放在外层而没有 aweme_detail 包装的情况
                aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data
                
                author_info = aweme_detail.get("author", {})
                nickname = author_info.get("nickname", "未知作者")
                sec_uid = author_info.get("sec_uid", "")
                desc = aweme_detail.get("desc", "无描述")
                
                if sec_uid:
                    author_url = f"https://www.douyin.com/user/{sec_uid}"
                    caption = f"<a href='{author_url}'>{nickname}</a>\n{desc}"
                else:
                    caption = f"<b>{nickname}</b>\n{desc}"

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
                        
                    except (TelegramEntityTooLarge, TelegramBadRequest, asyncio.TimeoutError) as e:
                        try:
                            temp_filename = f"video_{message.message_id}.mp4"
                            temp_filepath = os.path.join("/var/lib/telegram-bot-api", temp_filename)
                            
                            async with client.stream("GET", video_url, timeout=300.0) as video_response:
                                video_response.raise_for_status()
                                with open(temp_filepath, "wb") as f:
                                    async for chunk in video_response.aiter_bytes():
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
                                request_timeout=300
                            )
                            await message.delete()
                            
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                                
                        except Exception as sub_e:
                            print(f"Channel Fallback download error: {str(sub_e)}")
                            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                                os.remove(temp_filepath)

        except Exception as e:
            print(f"Channel Error occurred: {str(e)}")

async def main():
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())