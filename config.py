import os
import sys
import logging
import tempfile

# =======================
# Logging Configuration
# =======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("DouyinBot")

# =======================
# Environment Variables
# =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN 环境变量未设置，程序即将退出。")
    sys.exit(1)

# API & Engine Settings
API_BASE_URL = os.environ.get("API_BASE_URL", "")
LOCAL_API_SERVER = os.environ.get("LOCAL_API_SERVER")
UPSTREAM_API_PATH = os.environ.get("UPSTREAM_API_PATH", "/app/upstream_api")

# Directory for temporary file buffering (supports Local Bot API Server or fallback to system temp)
_default_temp = "/var/lib/telegram-bot-api" if LOCAL_API_SERVER else os.path.join(tempfile.gettempdir(), "douyin_bot")
TEMP_DIR = os.environ.get("TEMP_DIR", _default_temp)

try:
    os.makedirs(TEMP_DIR, exist_ok=True)
except Exception as e:
    logger.warning(f"无法创建指定 TEMP_DIR ({TEMP_DIR}): {e}，将自动回退到系统临时目录。")
    TEMP_DIR = os.path.join(tempfile.gettempdir(), "douyin_bot")
    os.makedirs(TEMP_DIR, exist_ok=True)

# Cookie Configuration (Single point of truth)
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", None)
TIKTOK_COOKIE = os.environ.get("TIKTOK_COOKIE", None)

# =======================
# Security & Whitelist
# =======================
ALLOWED_CHAT_IDS_STR = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = []

if ALLOWED_CHAT_IDS_STR:
    for x in ALLOWED_CHAT_IDS_STR.split(","):
        x = x.strip()
        if x:
            try:
                ALLOWED_CHAT_IDS.append(int(x))
            except ValueError:
                logger.critical(f"白名单配置安全错误: '{x}' 不是一个有效的整数 ID。为防止恶意用户访问，程序拒绝启动。请修复 ALLOWED_CHAT_IDS 配置。")
                sys.exit(1)

if ALLOWED_CHAT_IDS:
    logger.info(f"安全白名单已开启，允许访问的 ID: {ALLOWED_CHAT_IDS}")
else:
    logger.warning("安全白名单未开启，任何用户均可访问此 Bot！")
