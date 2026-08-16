import os
import time
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

from config import TEMP_DIR, LOCAL_API_STORAGE_ENABLED, logger
from crawler_service import ParsedResult, MediaAsset

_shared_client: Optional[httpx.AsyncClient] = None
MAX_ASSET_SIZE = 50 * 1024 * 1024  # 50MB per media item in memory buffer
MAX_VIDEO_FILE_SIZE = 2000 * 1024 * 1024  # ~1.95GB (严格限制在 Telegram Local API 2GB 上限内)
_ASSET_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(5)
_background_tasks: set[asyncio.Task] = set()


def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
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


async def safe_delayed_remove(filepath: str, delay: int = 60):
    """Safely delete temporary file after a delay to ensure Local API Server finished reading."""
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug(f"已安全清理临时视频文件: {filepath}")
    except Exception as e:
        logger.debug(f"延迟清理临时文件失败 ({filepath}): {e}")


def schedule_delayed_cleanup(filepath: str, delay: int = 60):
    """Safely schedule background delayed cleanup holding a strong task reference to prevent GC."""
    task = asyncio.create_task(safe_delayed_remove(filepath, delay=delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def cleanup_temp_dir(max_age_seconds: int = 3600):
    """Clean up orphaned temporary files older than max_age_seconds on startup or background."""
    try:
        if not os.path.exists(TEMP_DIR):
            return
        now = time.time()
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith("video_") or fname.startswith(".perm_test_"):
                fpath = os.path.join(TEMP_DIR, fname)
                if os.path.isfile(fpath):
                    try:
                        if now - os.path.getmtime(fpath) > max_age_seconds:
                            os.remove(fpath)
                            logger.info(f"清理过期临时文件: {fpath}")
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"清理临时目录 ({TEMP_DIR}) 异常: {e}")


def build_caption(title: str, canonical_url: str, max_length: int = 900) -> str:
    """
    Build safe HTML caption with canonical hyperlink and strict boundary check.
    Guarantees result length <= max_length (Telegram hard limit is 1024 chars).
    """
    raw_url = (canonical_url or "").strip()
    raw_title = (title or "").strip()

    # 1. URL 长度保护（防止异常超长 query/token 挤占全部预算）
    if len(raw_url) > 350:
        clean_url = raw_url.split("?")[0]
        raw_url = clean_url if len(clean_url) <= 350 else raw_url[:347] + "..."

    safe_url = html.escape(raw_url, quote=True)

    if not raw_title:
        return f'<a href="{safe_url}">点击查看原内容</a>'

    # 2. 计算 HTML 标签固定开销与可用文本预算
    template_overhead = len(f'<a href="{safe_url}"></a>')
    max_desc_len = max_length - template_overhead

    if max_desc_len <= 10:
        return f'<a href="{safe_url}">原链接</a>'

    # 3. 动态安全截断：若超长则预截断并加省略号，转义后若超限则逐步缩减原文字符
    is_truncated = len(raw_title) > max_desc_len
    cutoff = min(len(raw_title), max_desc_len - (3 if is_truncated else 0))
    truncated_text = (raw_title[:cutoff] + "...") if is_truncated else raw_title
    safe_desc = html.escape(truncated_text)

    while len(safe_desc) > max_desc_len and cutoff > 10:
        overflow = len(safe_desc) - max_desc_len
        step = max(5, int(overflow * 1.2))
        cutoff = max(10, cutoff - step)
        truncated_text = raw_title[:cutoff] + "..."
        safe_desc = html.escape(truncated_text)

    caption = f'<a href="{safe_url}">{safe_desc}</a>'

    # 4. 极端边界硬防线
    if len(caption) > 1000:
        caption = f'<a href="{safe_url}">{html.escape(raw_title[:30])}...</a>'

    return caption


async def _download_asset_to_buffer(client: httpx.AsyncClient, asset: MediaAsset, idx: int) -> Optional[dict]:
    """Download single media asset into memory buffer with size and concurrency limits."""
    async with _ASSET_DOWNLOAD_SEMAPHORE:
        try:
            async with client.stream(
                "GET",
                asset.url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=30.0
            ) as resp:
                if resp.status_code != 200:
                    return None

                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > MAX_ASSET_SIZE:
                    logger.warning(f"资源体积超出内存限制 ({content_length} bytes)，跳过下载: {asset.url}")
                    return None

                content = bytearray()
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_ASSET_SIZE:
                        logger.warning(f"流式下载超出最大限制 ({MAX_ASSET_SIZE} bytes)，中断: {asset.url}")
                        return None

                ext = "mp4" if asset.type == "video" else "jpg"
                return {
                    "bytes": bytes(content),
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

    # Safety guard: cap max assets per request to prevent resource exhaustion
    limited_assets = media_assets[:30]

    for i in range(0, len(limited_assets), 10):
        chunk = limited_assets[i:i + 10]
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
                err_msg = str(e).lower()
                if ("caption" in err_msg or "too long" in err_msg) and caption:
                    logger.warning(f"单媒体 Caption 发送异常 ({e})，降级重试...")
                    try:
                        if item["type"] == "photo":
                            await bot.send_photo(
                                chat_id=chat_id,
                                photo=file_obj,
                                caption=None,
                                reply_to_message_id=reply_to_msg_id if i == 0 else None,
                                request_timeout=120
                            )
                        else:
                            await bot.send_video(
                                chat_id=chat_id,
                                video=file_obj,
                                caption=None,
                                reply_to_message_id=reply_to_msg_id if i == 0 else None,
                                request_timeout=120,
                                **kwargs
                            )
                        success = True
                    except Exception as inner_e:
                        logger.error(f"降级发送单媒体失败: {inner_e}", exc_info=True)
                else:
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
                err_msg = str(e).lower()
                if ("caption" in err_msg or "too long" in err_msg) and caption:
                    logger.warning(f"媒体组 Caption 发送异常 ({e})，去除 Caption 降级重试...")
                    try:
                        fallback_mg = MediaGroupBuilder()
                        for item in valid_buffers:
                            file_obj = BufferedInputFile(item["bytes"], filename=item["filename"])
                            if item["type"] == "photo":
                                fallback_mg.add_photo(media=file_obj)
                            else:
                                kw = {}
                                if item.get("width"):
                                    kw["width"] = int(item["width"])
                                if item.get("height"):
                                    kw["height"] = int(item["height"])
                                fallback_mg.add_video(media=file_obj, **kw)
                        await bot.send_media_group(
                            chat_id=chat_id,
                            media=fallback_mg.build(),
                            reply_to_message_id=reply_to_msg_id if i == 0 else None,
                            request_timeout=120
                        )
                        success = True
                    except Exception as inner_e:
                        logger.error(f"降级发送媒体组失败: {inner_e}", exc_info=True)
                else:
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
    send_success = False
    try:
        async with client.stream(
            "GET",
            video_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=600.0
        ) as resp:
            resp.raise_for_status()

            header_len = resp.headers.get("content-length")
            if header_len and int(header_len) > MAX_VIDEO_FILE_SIZE:
                raise ValueError(f"视频文件体积超出 2GB 上限 ({header_len} bytes)，拒绝下载")

            downloaded_size = 0
            async with aiofiles.open(temp_filepath, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    downloaded_size += len(chunk)
                    if downloaded_size > MAX_VIDEO_FILE_SIZE:
                        raise ValueError(f"视频流式下载超出 2GB 限制 ({MAX_VIDEO_FILE_SIZE} bytes)，已中止")
                    await f.write(chunk)

        # Update chat action during upload
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass

        local_video_file = FSInputFile(temp_filepath)
        try:
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
            send_success = True
            return True
        except Exception as e:
            err_msg = str(e).lower()
            if "caption" in err_msg or "too long" in err_msg:
                logger.warning(f"Caption 发送异常 ({e})，降级为简短 Caption 重试...")
                fallback_caption = caption[:200] + "..." if caption else None
                await bot.send_video(
                    chat_id=chat_id,
                    video=local_video_file,
                    caption=fallback_caption,
                    thumbnail=thumbnail_file,
                    width=width,
                    height=height,
                    reply_to_message_id=reply_to_msg_id,
                    supports_streaming=True,
                    request_timeout=600
                )
                send_success = True
                return True
            raise e
    except Exception as e:
        logger.error(f"Failed to send video: {e}", exc_info=True)
        return False
    finally:
        # 采用强引用任务调度的智能延迟清理
        if not os.path.exists(temp_filepath):
            pass
        elif not send_success:
            # 下载或发送异常，立即清理残余半成品
            schedule_delayed_cleanup(temp_filepath, delay=0)
        elif LOCAL_API_STORAGE_ENABLED:
            # Local API Server 模式：按文件大小动态计算延时（基准 120 秒 + 每 100MB 延时 30 秒，上限 600 秒）
            file_size_mb = os.path.getsize(temp_filepath) / (1024 * 1024)
            delay = min(600, max(120, int(120 + (file_size_mb / 100) * 30)))
            schedule_delayed_cleanup(temp_filepath, delay=delay)
        else:
            # 传统流式传输已结束，延时 10 秒安全清理
            schedule_delayed_cleanup(temp_filepath, delay=10)


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
