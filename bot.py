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
                aweme_detail = root_data.get("aweme_detail", root_data)
                
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
                        img_url = img.get("url_list", [])[0] if img.get("url_list") else None
                        if img_url:
                            media_assets.append({"type": "photo", "url": img_url})

                    if media_assets:
                        try:
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=30.0) as media_client:
                                for i in range(0, len(media_assets), 10):
                                    chunk = media_assets[i:i+10]
                                    media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
                                    
                                    download_tasks = [media_client.get(asset["url"]) for asset in chunk]
                                    responses = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    buffer_list = []
                                    for idx, res in enumerate(responses):
                                        if isinstance(res, httpx.Response) and res.status_code == 200:
                                            asset_type = chunk[idx]["type"]
                                            ext = "webp" if asset_type == "photo" else "mp4"
                                            buffer_list.append((res.content, f"media_{i}_{idx}.{ext}", asset_type))
                                            
                                    for buf_bytes, filename, a_type in buffer_list:
                                        file_obj = BufferedInputFile(buf_bytes, filename=filename)
                                        if a_type == "photo":
                                            media_group.add_photo(media=file_obj)
                                        else:
                                            media_group.add_video(media=file_obj)
                                    
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
                
                # 提取视频原始分辨率参数给 Telegram 强制适配比例
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

                video_url = None
                max_width = 0
                max_bitrate = 0
                
                # 双因子排序：首选分辨率（width）最高的，同分辨率下选码率（bit_rate）最高的
                bit_rate_list = video_info.get("bit_rate", [])
                for rate in bit_rate_list:
                    play_addr = rate.get("play_addr", {})
                    current_width = play_addr.get("width", 0)
                    current_bitrate = rate.get("bit_rate", 0)
                    url_list = play_addr.get("url_list", [])
                    
                    if not url_list:
                        continue

                    # 判断逻辑：
                    # 1. 如果分辨率更大，直接替换
                    # 2. 如果分辨率相同，但码率更大，也替换
                    if current_width > max_width or (current_width == max_width and current_bitrate > max_bitrate):
                        max_width = current_width
                        max_bitrate = current_bitrate
                        video_url = url_list[0]
                
                if not video_url:
                    play_addr = video_info.get("play_addr", {})
                    url_list = play_addr.get("url_list", [])
                    if url_list:
                        video_url = url_list[0]
                        
                if not video_url:
                    video_dict = root_data.get("video_data", {})
                    video_url = video_dict.get("nwm_video_url_HQ") or video_dict.get("nwm_video_url")
                
                if video_url and "/playwm/" in video_url:
                     video_url = video_url.replace("/playwm/", "/play/")
                     
                if video_url:
                    try:
                        video_file = URLInputFile(video_url)
                        await bot.send_video(
                            chat_id=message.chat.id,
                            video=video_file,
                            caption=caption,
                            thumbnail=thumbnail_file,
                            width=vid_width,      # 强制指定宽度
                            height=vid_height,    # 强制指定高度
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
                                width=vid_width,      # 强制指定宽度
                                height=vid_height,    # 强制指定高度
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
                aweme_detail = root_data.get("aweme_detail", root_data)
                
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
                        img_url = img.get("url_list", [])[0] if img.get("url_list") else None
                        if img_url:
                            media_assets.append({"type": "photo", "url": img_url})

                    if media_assets:
                        try:
                            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=30.0) as media_client:
                                for i in range(0, len(media_assets), 10):
                                    chunk = media_assets[i:i+10]
                                    media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
                                    
                                    download_tasks = [media_client.get(asset["url"]) for asset in chunk]
                                    responses = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    buffer_list = []
                                    for idx, res in enumerate(responses):
                                        if isinstance(res, httpx.Response) and res.status_code == 200:
                                            asset_type = chunk[idx]["type"]
                                            ext = "webp" if asset_type == "photo" else "mp4"
                                            buffer_list.append((res.content, f"media_{i}_{idx}.{ext}", asset_type))
                                            
                                    for buf_bytes, filename, a_type in buffer_list:
                                        file_obj = BufferedInputFile(buf_bytes, filename=filename)
                                        if a_type == "photo":
                                            media_group.add_photo(media=file_obj)
                                        else:
                                            media_group.add_video(media=file_obj)
                                    
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
                
                # 提取视频原始分辨率参数给 Telegram 强制适配比例
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

                video_url = None
                max_width = 0
                max_bitrate = 0
                
                # 双因子排序：首选分辨率（width）最高的，同分辨率下选码率（bit_rate）最高的
                bit_rate_list = video_info.get("bit_rate", [])
                for rate in bit_rate_list:
                    play_addr = rate.get("play_addr", {})
                    current_width = play_addr.get("width", 0)
                    current_bitrate = rate.get("bit_rate", 0)
                    url_list = play_addr.get("url_list", [])
                    
                    if not url_list:
                        continue

                    # 判断逻辑：
                    # 1. 如果分辨率更大，直接替换
                    # 2. 如果分辨率相同，但码率更大，也替换
                    if current_width > max_width or (current_width == max_width and current_bitrate > max_bitrate):
                        max_width = current_width
                        max_bitrate = current_bitrate
                        video_url = url_list[0]
                
                if not video_url:
                    play_addr = video_info.get("play_addr", {})
                    url_list = play_addr.get("url_list", [])
                    if url_list:
                        video_url = url_list[0]
                        
                if not video_url:
                    video_dict = root_data.get("video_data", {})
                    video_url = video_dict.get("nwm_video_url_HQ") or video_dict.get("nwm_video_url")
                
                if video_url and "/playwm/" in video_url:
                     video_url = video_url.replace("/playwm/", "/play/")
                     
                if video_url:
                    try:
                        video_file = URLInputFile(video_url)
                        await bot.send_video(
                            chat_id=message.chat.id,
                            video=video_file,
                            caption=caption,
                            thumbnail=thumbnail_file,
                            width=vid_width,      # 强制指定宽度
                            height=vid_height,    # 强制指定高度
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
                                width=vid_width,      # 强制指定宽度
                                height=vid_height,    # 强制指定高度
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