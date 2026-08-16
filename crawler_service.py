import os
import re
import sys
import html
import httpx
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Union
from collections.abc import Sequence

from config import DOUYIN_COOKIE, TIKTOK_COOKIE, API_BASE_URL, UPSTREAM_API_PATH, logger

# =======================
# Compatibility Shim for Upstream Crawler (httpx >= 0.28 proxies kwarg removal)
# =======================
if not getattr(httpx, "_proxies_shim_applied", False):
    _orig_async_init = httpx.AsyncClient.__init__
    def _patched_async_init(self, *args, **kwargs):
        if "proxies" in kwargs:
            proxies = kwargs.pop("proxies")
            if proxies and "proxy" not in kwargs:
                if isinstance(proxies, str):
                    kwargs["proxy"] = proxies
                elif isinstance(proxies, dict):
                    kwargs["proxy"] = (
                        proxies.get("all://")
                        or proxies.get("https://")
                        or proxies.get("http://")
                        or proxies.get("https")
                        or proxies.get("http")
                    )
        return _orig_async_init(self, *args, **kwargs)

    _orig_sync_init = httpx.Client.__init__
    def _patched_sync_init(self, *args, **kwargs):
        if "proxies" in kwargs:
            proxies = kwargs.pop("proxies")
            if proxies and "proxy" not in kwargs:
                if isinstance(proxies, str):
                    kwargs["proxy"] = proxies
                elif isinstance(proxies, dict):
                    kwargs["proxy"] = (
                        proxies.get("all://")
                        or proxies.get("https://")
                        or proxies.get("http://")
                        or proxies.get("https")
                        or proxies.get("http")
                    )
        return _orig_sync_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _patched_async_init
    httpx.Client.__init__ = _patched_sync_init
    httpx._proxies_shim_applied = True

# =======================
# Data Classes
# =======================
@dataclass
class MediaAsset:
    type: str  # "photo" or "video"
    url: str
    width: Optional[int] = None
    height: Optional[int] = None

@dataclass
class ParsedResult:
    title: str
    canonical_url: str
    media_type: str  # "video" or "image"
    media_assets: List[MediaAsset] = field(default_factory=list)
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    aweme_id: Optional[str] = None

# =======================
# Helper: Extract ID & Build Canonical URL
# =======================
AWEME_ID_REGEX = re.compile(r'(?:video/|note/|photo/|share/video/|/item/|modal_id=)(\d{18,21})')
DIGITS_REGEX = re.compile(r'\b(\d{18,21})\b')
TIKTOK_USER_REGEX = re.compile(r'@([A-Za-z0-9_.-]+)')
BILIBILI_ID_REGEX = re.compile(r'(?:video/|/)(BV[0-9A-Za-z]{10}|av\d+)', re.IGNORECASE)

async def resolve_canonical_info(
    raw_url: str,
    aweme_id: Optional[str] = None,
    unique_id: Optional[str] = None,
    is_image: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[Optional[str], str]:
    """Extract standard media ID and construct a clean canonical desktop URL."""
    extracted_id = aweme_id
    final_url = raw_url

    # 1. If it's a short URL, follow redirect to extract real ID & final URL
    short_domains = ["v.douyin.com", "vm.tiktok.com", "vt.tiktok.com", "b23.tv"]
    if any(short in raw_url.lower() for short in short_domains):
        try:
            should_close = False
            req_client = client
            if req_client is None:
                req_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
                should_close = True
            
            resp = await req_client.head(
                raw_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                follow_redirects=True
            )
            final_url = str(resp.url)
            
            if should_close:
                await req_client.aclose()
        except Exception as e:
            logger.debug(f"Follow redirect failed for {raw_url}: {e}")

    # 2. Try extracting ID if not provided
    if not extracted_id:
        if "bilibili" in final_url.lower() or "b23.tv" in raw_url.lower():
            bili_m = BILIBILI_ID_REGEX.search(final_url) or BILIBILI_ID_REGEX.search(raw_url)
            if bili_m:
                extracted_id = bili_m.group(1)
        else:
            m = AWEME_ID_REGEX.search(final_url) or AWEME_ID_REGEX.search(raw_url) or DIGITS_REGEX.search(final_url)
            if m:
                extracted_id = m.group(1)

    # 3. Construct clean canonical URL based on domain
    lower_target = final_url.lower() + " " + raw_url.lower()
    is_tiktok = "tiktok" in lower_target
    is_bilibili = "bilibili" in lower_target or "b23.tv" in lower_target
    is_image = is_image or ("/note/" in lower_target) or ("/photo/" in lower_target)
    
    if is_bilibili:
        if extracted_id:
            canonical_url = f"https://www.bilibili.com/video/{extracted_id}"
        else:
            canonical_url = final_url.split('?')[0]
    elif is_tiktok:
        if extracted_id:
            user = unique_id
            if not user:
                user_m = TIKTOK_USER_REGEX.search(final_url) or TIKTOK_USER_REGEX.search(raw_url)
                user = user_m.group(1) if user_m else "user"
            action = "photo" if is_image else "video"
            canonical_url = f"https://www.tiktok.com/@{user}/{action}/{extracted_id}"
        else:
            canonical_url = final_url.split('?')[0]
    else:
        # Default to Douyin
        if extracted_id:
            action = "note" if is_image else "video"
            canonical_url = f"https://www.douyin.com/{action}/{extracted_id}"
        else:
            canonical_url = final_url.split('?')[0]

    return extracted_id, canonical_url


def pick_best_video_url(video_info: Optional[dict], root_data: Optional[dict] = None) -> Optional[str]:
    """Select the highest resolution/bitrate video stream."""
    if not video_info and not root_data:
        return None

    video_info = video_info or {}
    video_url = None
    max_width = 0
    max_bitrate = 0

    bit_rate_list = video_info.get("bit_rate") or []
    for rate in bit_rate_list:
        if not isinstance(rate, dict):
            continue
        play_addr = rate.get("play_addr") or {}
        current_width = play_addr.get("width", 0) or 0
        current_bitrate = rate.get("bit_rate", 0) or 0
        url_list = play_addr.get("url_list") or []

        if not url_list:
            continue

        if current_width > max_width or (current_width == max_width and current_bitrate > max_bitrate):
            max_width = current_width
            max_bitrate = current_bitrate
            video_url = url_list[0]

    if not video_url:
        play_addr = video_info.get("play_addr") or {}
        url_list = play_addr.get("url_list") or []
        if url_list:
            video_url = url_list[0]

    if not video_url:
        download_addr = video_info.get("download_addr") or {}
        url_list = download_addr.get("url_list") or []
        if url_list:
            video_url = url_list[0]

    if not video_url and root_data:
        video_dict = root_data.get("video_data") or {}
        video_url = video_dict.get("nwm_video_url_HQ") or video_dict.get("nwm_video_url")

    if video_url and "/playwm/" in video_url:
        video_url = video_url.replace("/playwm/", "/play/")

    return video_url


async def _extract_aweme_result(
    raw_url: str,
    aweme_data: dict,
    root_data: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None
) -> Optional[ParsedResult]:
    """Unified helper to parse Aweme data dict into ParsedResult."""
    if not aweme_data or not isinstance(aweme_data, dict):
        return None

    raw_desc = aweme_data.get("desc", "")
    aweme_id = str(aweme_data.get("aweme_id") or aweme_data.get("id") or (root_data.get("aweme_id") if root_data else "") or "")
    
    author_info = aweme_data.get("author") or {}
    unique_id = author_info.get("unique_id") or author_info.get("short_id") or author_info.get("nickname")

    # Check images (standard Douyin or TikTok format)
    images = aweme_data.get("images") or []
    if not images and "image_post_info" in aweme_data:
        img_list = (aweme_data.get("image_post_info") or {}).get("images", [])
        for im in img_list:
            if isinstance(im, dict):
                d_url = ((im.get("display_image") or {}).get("url_list") or [None])[0]
                if d_url:
                    images.append({"url_list": [d_url]})

    if images:
        extracted_id, canonical_url = await resolve_canonical_info(
            raw_url, aweme_id=aweme_id, unique_id=unique_id, is_image=True, client=client
        )
        assets = []
        for img in images:
            if not isinstance(img, dict):
                continue
            live_video = img.get("video") or {}
            live_video_url = pick_best_video_url(live_video) if live_video else None

            if live_video_url:
                assets.append(MediaAsset(
                    type="video",
                    url=live_video_url,
                    width=img.get("width"),
                    height=img.get("height")
                ))
            else:
                url_list = (
                    img.get("url_list")
                    or (img.get("display_image", {}).get("url_list") if isinstance(img.get("display_image"), dict) else None)
                    or img.get("download_url_list")
                    or []
                )
                if url_list:
                    assets.append(MediaAsset(
                        type="photo",
                        url=url_list[-1],
                        width=img.get("width"),
                        height=img.get("height")
                    ))
        
        if not assets:
            return None

        return ParsedResult(
            title=raw_desc,
            canonical_url=canonical_url,
            media_type="image",
            media_assets=assets,
            aweme_id=extracted_id
        )
    else:
        # Video format
        video_info = aweme_data.get("video") or {}
        video_url = pick_best_video_url(video_info, root_data=root_data or aweme_data)
        if not video_url:
            return None

        cover_url = None
        cover_obj = video_info.get("cover") or video_info.get("origin_cover") or {}
        cover_url_list = cover_obj.get("url_list") or []
        if cover_url_list:
            cover_url = cover_url_list[0]

        vid_width = video_info.get("width")
        vid_height = video_info.get("height")

        extracted_id, canonical_url = await resolve_canonical_info(
            raw_url, aweme_id=aweme_id, unique_id=unique_id, is_image=False, client=client
        )

        return ParsedResult(
            title=raw_desc,
            canonical_url=canonical_url,
            media_type="video",
            video_url=video_url,
            cover_url=cover_url,
            video_width=vid_width,
            video_height=vid_height,
            aweme_id=extracted_id
        )


# =======================
# Engine 1: Built-in Evil0ctal Crawler
# =======================
class BuiltinCrawlerEngine:
    def __init__(self):
        self.available = False
        self._setup_upstream()

    def _setup_upstream(self):
        """Check and import Evil0ctal upstream crawler modules."""
        paths_to_check = [
            UPSTREAM_API_PATH,
            "/tmp/Douyin_TikTok_Download_API",
            os.path.abspath(os.path.join(os.path.dirname(__file__), "upstream_api")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Douyin_TikTok_Download_API")),
        ]
        
        found_path = None
        for p in paths_to_check:
            if os.path.exists(os.path.join(p, "crawlers")):
                found_path = p
                break

        if not found_path:
            logger.info("未检测到内置 Evil0ctal 爬虫目录，将使用 ParseHub / 外部 API 引擎。")
            return

        if found_path not in sys.path:
            sys.path.insert(0, found_path)

        try:
            # Import upstream modules
            import crawlers.douyin.web.web_crawler as dy_mod
            import crawlers.tiktok.web.web_crawler as tt_mod
            import crawlers.tiktok.app.app_crawler as tt_app_mod
            from crawlers.hybrid.hybrid_crawler import HybridCrawler

            # Dynamically inject cookies
            if DOUYIN_COOKIE and hasattr(dy_mod, "config") and "TokenManager" in dy_mod.config:
                dy_mod.config["TokenManager"]["douyin"]["headers"]["Cookie"] = DOUYIN_COOKIE
                logger.info("已成功注入 DOUYIN_COOKIE 到内置爬虫引擎。")

            if TIKTOK_COOKIE:
                if hasattr(tt_mod, "config") and "TokenManager" in tt_mod.config:
                    tt_mod.config["TokenManager"]["tiktok"]["headers"]["Cookie"] = TIKTOK_COOKIE
                if hasattr(tt_app_mod, "config") and "TokenManager" in tt_app_mod.config:
                    tt_app_mod.config["TokenManager"]["tiktok"]["headers"]["Cookie"] = TIKTOK_COOKIE
                logger.info("已成功注入 TIKTOK_COOKIE 到内置爬虫引擎。")

            self.hybrid_crawler = HybridCrawler()
            self.available = True
            logger.info(f"内置 Evil0ctal 爬虫引擎初始化成功 (路径: {found_path})！")
        except Exception as e:
            logger.warning(f"内置 Evil0ctal 爬虫引擎加载失败: {e}，将使用降级引擎。")
            self.available = False

    async def parse(self, url: str, client: httpx.AsyncClient) -> Optional[ParsedResult]:
        if not self.available:
            return None

        try:
            # Direct hybrid parsing with minimal=False for full details
            data = await self.hybrid_crawler.hybrid_parsing_single_video(url, minimal=False)
            return await _extract_aweme_result(url, data, root_data=data, client=client)
        except Exception as e:
            logger.debug(f"内置 Evil0ctal 引擎解析失败 ({url}): {e}")
            return None


# =======================
# Engine 2: External API (Optional)
# =======================
async def parse_with_external_api(url: str, client: httpx.AsyncClient) -> Optional[ParsedResult]:
    if not API_BASE_URL:
        return None

    try:
        api_endpoint = f"{API_BASE_URL.rstrip('/')}/api/hybrid/video_data"
        response = await client.get(
            api_endpoint,
            params={"url": url, "minimal": "false"},
            timeout=15.0
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 200:
            return None

        root_data = data.get("data") or {}
        aweme_detail = root_data.get("aweme_detail") if "aweme_detail" in root_data else root_data

        return await _extract_aweme_result(url, aweme_detail, root_data=root_data, client=client)
    except Exception as e:
        logger.debug(f"外部 API 解析失败 ({url}): {e}")
        return None


# =======================
# Engine 3: ParseHub Fallback Engine
# =======================
async def parse_with_parsehub(url: str, client: httpx.AsyncClient) -> Optional[ParsedResult]:
    try:
        from parsehub import ParseHub
        from parsehub.types.media_ref import VideoRef, ImageRef, LivePhotoRef, AniRef

        # Select appropriate cookie based on target platform
        cookie = TIKTOK_COOKIE if "tiktok" in url.lower() else DOUYIN_COOKIE

        ph = ParseHub()
        result = await ph.parse(url, cookie=cookie)
        if not result or not result.media:
            return None

        media_list = list(result.media) if isinstance(result.media, Sequence) else [result.media]
        if not media_list:
            return None

        raw_desc = result.title or ""
        extracted_id = getattr(result, "id", None) or getattr(result, "media_id", None)

        is_video = isinstance(media_list[0], (VideoRef, AniRef))

        if not is_video:
            # Images or LivePhotos
            assets = []
            for m in media_list:
                if isinstance(m, LivePhotoRef) and m.video_url:
                    assets.append(MediaAsset(
                        type="video",
                        url=m.video_url,
                        width=getattr(m, "width", None),
                        height=getattr(m, "height", None)
                    ))
                else:
                    assets.append(MediaAsset(
                        type="photo",
                        url=m.url,
                        width=getattr(m, "width", None),
                        height=getattr(m, "height", None)
                    ))

            extracted_id, canonical_url = await resolve_canonical_info(
                url, aweme_id=str(extracted_id) if extracted_id else None, is_image=True, client=client
            )

            return ParsedResult(
                title=raw_desc,
                canonical_url=canonical_url,
                media_type="image",
                media_assets=assets,
                aweme_id=extracted_id
            )
        else:
            # Video
            v_ref = media_list[0]
            video_url = v_ref.url
            cover_url = getattr(v_ref, "thumb_url", None)
            vid_width = getattr(v_ref, "width", None)
            vid_height = getattr(v_ref, "height", None)

            extracted_id, canonical_url = await resolve_canonical_info(
                url, aweme_id=str(extracted_id) if extracted_id else None, is_image=False, client=client
            )

            return ParsedResult(
                title=raw_desc,
                canonical_url=canonical_url,
                media_type="video",
                video_url=video_url,
                cover_url=cover_url,
                video_width=vid_width,
                video_height=vid_height,
                aweme_id=extracted_id
            )

    except Exception as e:
        logger.debug(f"ParseHub 解析失败 ({url}): {e}")
        return None


# =======================
# Unified Dispatcher
# =======================
_builtin_engine = None

def get_builtin_engine() -> BuiltinCrawlerEngine:
    global _builtin_engine
    if _builtin_engine is None:
        _builtin_engine = BuiltinCrawlerEngine()
    return _builtin_engine


async def parse_url_multi_engine(url: str, client: httpx.AsyncClient) -> Optional[ParsedResult]:
    """
    Parse a Douyin/TikTok URL using the prioritized multi-engine strategy:
    1. Built-in Evil0ctal Crawler (Internal memory execution, highest quality)
    2. External API (if API_BASE_URL configured)
    3. Built-in ParseHub Engine (Graceful fallback)
    """
    # 1. Try Built-in Evil0ctal
    engine = get_builtin_engine()
    res = await engine.parse(url, client)
    if res:
        logger.info(f"成功使用 [内置 Evil0ctal 引擎] 解析: {res.canonical_url}")
        return res

    # 2. Try External API
    if API_BASE_URL:
        res = await parse_with_external_api(url, client)
        if res:
            logger.info(f"成功使用 [外部 API 引擎] 解析: {res.canonical_url}")
            return res

    # 3. Try ParseHub Fallback
    res = await parse_with_parsehub(url, client)
    if res:
        logger.info(f"成功使用 [内置 ParseHub 降级引擎] 解析: {res.canonical_url}")
        return res

    logger.warning(f"所有引擎解析均失败: {url}")
    return None
