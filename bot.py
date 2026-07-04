import os
import re
import io
import httpx
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, URLInputFile, FSInputFile, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramEntityTooLarge, TelegramBadRequest
from aiogram.utils.media_group import MediaGroupBuilder

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_BASE_URL = os.environ.get("API_BASE_URL", "https://douyin.wtf")
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER")

if LOCAL_API_SERVER:
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(LOCAL_API_SERVER, is_local=True)
    )
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp = Dispatcher()

URL_REGEX = re.compile(r"https?://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.reply("发送带有抖音/TikTok分享链接的消息给我，我会为你提取无水印视频或图集。")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.reply("直接向我发送包含抖音或TikTok分享链接的消息即可，支持视频和图集解析。")

# 移除对 channel 的支持，因为频道里的消息没有 message_id，无法用 reply() 回复，需要重写大量逻辑
# 但实际上，在 channel 里，机器人是作为一个发布者存在的，而不是一个交互对象。
# 这意味着你发链接，它解析，然后作为频道消息发出。

@dp.message(F.text)
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
                
                bit_rate_list = video_info.get("bit_rate", [])
                for rate in bit_rate_list:
                    play_addr = rate.get("play_addr", {})
                    width = play_addr.get("width", 0)
                    url_list = play_addr.get("url_list", [])
                    
                    if width > max_width and url_list:
                        max_width = width
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

# --- 新增：频道消息处理器 ---
@dp.channel_post(F.text)
async def handle_channel_post(message: Message):
    urls = URL_REGEX.findall(message.text)
    if not urls:
        return
        
    # 在频道中也只响应带有特定域名的链接
    if not any(domain in urls[0].lower() for domain in ["douyin", "tiktok", "snssdk"]):
        return

    target_url = urls[0]
    
    # 频道中不发“正在处理”，因为频繁发/删消息在频道里体验不好，且可能触发限流
    # 我们直接静默下载，下完直接替换掉原来的那条包含链接的消息

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

                # ==========================
                # 频道 - 图集解析处理
                # ==========================
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
                            # 发送成功后，删除原帖
                            await message.delete()
                        except Exception as sub_e:
                            print(f"Channel MediaGroup send error: {sub_e}")
                        return

                # ==========================
                # 频道 - 视频解析处理
                # ==========================
                video_info = aweme_detail.get("video", {})
                
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
                
                bit_rate_list = video_info.get("bit_rate", [])
                for rate in bit_rate_list:
                    play_addr = rate.get("play_addr", {})
                    width = play_addr.get("width", 0)
                    url_list = play_addr.get("url_list", [])
                    
                    if width > max_width and url_list:
                        max_width = width
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
                            supports_streaming=True,
                            request_timeout=120
                        )
                        # 发送成功后删除带有链接的原帖
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
                                supports_streaming=True,
                                request_timeout=300
                            )
                            # 发送成功后删除带有链接的原帖
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
