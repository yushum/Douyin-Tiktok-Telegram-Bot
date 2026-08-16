import os
import html
import httpx
import asyncio
import uuid
import aiofiles
from typing import Optional
from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import URLInputFile, FSInputFile, BufferedInputFile
from aiogram.utils.media_group import MediaGroupBuilder

from config import TEMP_DIR, logger
from crawler_service import ParsedResult, MediaAsset

_shared_client: Optional[httpx.AsyncClient] = None

def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=60.0,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )
    return _shared_client

async def close_shared_client():
    """Gracefully close the shared httpx client."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None

def build_caption(title: str, canonical_url: str, max_length: int = 950) -> str:
    """Build safe HTML caption with canonical hyperlink and robust truncation for Telegram 1024 limit."""
    safe_url = html.escape(canonical_url or "", quote=True)
    raw_title = (title or "").strip()
    if not raw_title:
        return f'<a href="{safe_url}">点击查看原内容</a>'

    # Pre-truncate title if too long
    if len(raw_title) > 600:
        raw_title = raw_title[:597] + "..."

    safe_desc = html.escape(raw_title)
    caption = f'<a href="{safe_url}">{safe_desc}</a>'

    # Strict check against Telegram's 1024 char limit
    if len(caption) > max_length:
        safe_desc = html.escape(raw_title[:300]) + "..."
        caption = f'<a href="{safe_url}">{safe_desc}</a>'

    return caption

async def _download_asset_to_buffer(client: httpx.AsyncClient, asset: MediaAsset, idx: int):
    """Download single media asset into memory buffer."""
    try:
        res = await client.get(
            asset.url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=30.0
        )
        if res.status_code == 200:
            ext = "mp4" if asset.type == "video" else "jpg"
            return {
                "bytes": res.content,
                "filename": f"media_{idx}.{ext}",
                "type": asset.type,
                "width": asset.width,
                "height": asset.height,
            }
    except Exception as e:
        logger.debug(f"Download asset error: {e}")
    return None

async def send_image_group(
    bot: Bot,
    chat_id: int,
    media_assets: list[MediaAsset],
    caption: str,
    reply_to_msg_id: Optional[int] = None
) -> bool:
    """Concurrently download and send images/live photos as MediaGroups (or single item)."""
    client = get_shared_client()
    success = False

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    for i in range(0, len(media_assets), 10):
        chunk = media_assets[i:i + 10]
        download_tasks = [
            _download_asset_to_buffer(client, asset, i + idx)
            for idx, asset in enumerate(chunk)
        ]
        buffers = await asyncio.gather(*download_tasks, return_exceptions=True)
        valid_buffers = [b for b in buffers if isinstance(b, dict)]

        if not valid_buffers:
            continue

        if len(valid_buffers) == 1:
            # Telegram sendMediaGroup requires 2-10 items; single item must use send_photo / send_video
            item = valid_buffers[0]
            file_obj = BufferedInputFile(item["bytes"], filename=item["filename"])
            try:
                if item["type"] == "photo":
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=file_obj,
                        caption=caption if i == 0 else None,
                        reply_to_message_id=reply_to_msg_id if i == 0 else None,
                        request_timeout=120
                    )
                else:
                    kwargs = {}
                    if item.get("width"):
                        kwargs["width"] = int(item["width"])
                    if item.get("height"):
                        kwargs["height"] = int(item["height"])
                    await bot.send_video(
                        chat_id=chat_id,
                        video=file_obj,
                        caption=caption if i == 0 else None,
                        reply_to_message_id=reply_to_msg_id if i == 0 else None,
                        request_timeout=120,
                        **kwargs
                    )
                success = True
            except Exception as e:
                logger.error(f"Failed to send single media: {e}", exc_info=True)
        else:
            media_group = MediaGroupBuilder(caption=caption if i == 0 else None)
            for item in valid_buffers:
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

            try:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group.build(),
                    reply_to_message_id=reply_to_msg_id if i == 0 else None,
                    request_timeout=120
                )
                success = True
            except Exception as e:
                logger.error(f"Failed to send media group: {e}", exc_info=True)

    return success

async def send_video_media(
    bot: Bot,
    chat_id: int,
    video_url: str,
    cover_url: Optional[str],
    caption: str,
    width: Optional[int],
    height: Optional[int],
    reply_to_msg_id: Optional[int] = None
) -> bool:
    """Send video via direct URL or local direct upload with Local API."""
    client = get_shared_client()

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
    except Exception:
        pass

    # 1. Fetch thumbnail if present
    thumbnail_file = None
    if cover_url:
        try:
            cover_resp = await client.get(
                cover_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=10.0
            )
            if cover_resp.status_code == 200:
                thumbnail_file = BufferedInputFile(cover_resp.content, filename="cover.jpeg")
        except Exception:
            pass

    # 2. Check content-length for direct URL optimization (< 18MB)
    content_length = 0
    try:
        head_resp = await client.head(
            video_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10.0
        )
        content_length = int(head_resp.headers.get("content-length", 0))
    except Exception:
        pass

    # 3. Fast path: Direct URL sending if file is small
    if 0 < content_length <= 18 * 1024 * 1024:
        try:
            video_file = URLInputFile(video_url)
            await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                thumbnail=thumbnail_file,
                width=width,
                height=height,
                reply_to_message_id=reply_to_msg_id,
                supports_streaming=True,
                request_timeout=120
            )
            return True
        except Exception as e:
            logger.debug(f"Direct URL send failed, falling back to local download: {e}")

    # 4. Fallback/Large File path: Stream download to TEMP_DIR and upload via Local API Server
    temp_filename = f"video_{chat_id}_{uuid.uuid4().hex[:8]}.mp4"
    temp_filepath = os.path.join(TEMP_DIR, temp_filename)
    try:
        async with client.stream(
            "GET",
            video_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=600.0
        ) as resp:
            resp.raise_for_status()
            async with aiofiles.open(temp_filepath, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    await f.write(chunk)

        # Update chat action during upload
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass

        local_video_file = FSInputFile(temp_filepath)
        await bot.send_video(
            chat_id=chat_id,
            video=local_video_file,
            caption=caption,
            thumbnail=thumbnail_file,
            width=width,
            height=height,
            reply_to_message_id=reply_to_msg_id,
            supports_streaming=True,
            request_timeout=600
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send video: {e}", exc_info=True)
        return False
    finally:
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except Exception:
                pass

async def send_parsed_media(
    bot: Bot,
    chat_id: int,
    result: ParsedResult,
    reply_to_msg_id: Optional[int] = None
) -> bool:
    """Unified entrypoint to dispatch media sending."""
    caption = build_caption(result.title, result.canonical_url)

    if result.media_type == "image" and result.media_assets:
        return await send_image_group(
            bot=bot,
            chat_id=chat_id,
            media_assets=result.media_assets,
            caption=caption,
            reply_to_msg_id=reply_to_msg_id
        )
    elif result.media_type == "video" and result.video_url:
        return await send_video_media(
            bot=bot,
            chat_id=chat_id,
            video_url=result.video_url,
            cover_url=result.cover_url,
            caption=caption,
            width=result.video_width,
            height=result.video_height,
            reply_to_msg_id=reply_to_msg_id
        )
    return False
