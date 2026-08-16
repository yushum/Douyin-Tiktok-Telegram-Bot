# Douyin/TikTok Telegram Bot

![Docker Pulls](https://img.shields.io/docker/pulls/yushum/douyin-tiktok-telegram-bot?style=flat-square)
![Docker Image Size (tag)](https://img.shields.io/docker/image-size/yushum/douyin-tiktok-telegram-bot/latest?style=flat-square)
![GitHub License](https://img.shields.io/github/license/yushum/Douyin-Tiktok-Telegram-Bot?style=flat-square)
![Multi-Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)
![Docker Image Version (latest semver)](https://img.shields.io/docker/v/yushum/douyin-tiktok-telegram-bot?sort=semver&style=flat-square)

这是一个基于 Python `aiogram` 框架构建的高性能 Telegram 机器人，专门用于将抖音和 TikTok 的分享链接解析为无水印视频或图集，并在 Telegram 中原生发送。

## ✨ 核心特性

- **双容器极简架构**：内置集成核心爬虫与解析引擎，只需 `bot-api-server` 和 `tg-bot` 两个容器即可运行，告别额外的独立 API 容器和复杂的 yaml 挂载。
- **内置双引擎高可用**：
  - **主力引擎**：内置 Evil0ctal 核心爬虫，内存直接调用，支持超清 4K CDN 源文件与无水印图集提取；
  - **降级引擎**：内置 ParseHub，自动兜底风控或特殊格式链接。
- **单点凭证管理**：抖音和 TikTok Cookie 统一通过 `.env` 环境变量配置，不再需要重复配置多处文件。
- **纯净链接规范化**：自动追踪分享短链重定向并提取作品 ID，将超链接格式化为标准干净的 Web 播放页直链（拒绝带有冗余追踪参数的短链）。
- **实况与图集支持**：完美解析抖音图集和实况照片，以无损图片流 (MediaGroup) 形式原生发送。
- **2GB 大文件突破**：基于双容器共享数据卷架构打通 Telegram Local API Server，彻底突破官方 50MB 传输限制，最高支持 2GB 的长视频极速直传。
- **全场景适配**：私聊即时响应；支持频道静默发布与带链接原帖自动清理；群组模式下智能嗅探短链接，实现纯净防打扰。
- **全自动 CI/CD**：GitHub Actions 智能监听 Evil0ctal 与 ParseHub 上游更新，有更新自动构建 multi-arch (amd64/arm64) 镜像推送到 Docker Hub，零浪费算力。

## 📦 快速部署

### 1. 准备配置

创建一个目录用于存放数据和配置，并在目录内创建 `compose.yaml` 文件（直接复制本仓库的 `compose.yaml`），然后创建 `.env` 文件填入凭证：

```env
# 去 https://my.telegram.org 申请
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890

# 去 @BotFather 申请
BOT_TOKEN=123456789:ABCdefGHIjklmNOPQrstUVwxyZ

# (可选) 填入你的抖音/TikTok网页版 Cookie（用于防风控或解析需登录的内容）
DOUYIN_COOKIE=
TIKTOK_COOKIE=

# (可选) 外部备用 API 节点（默认留空，直接使用内置引擎）
API_BASE_URL=

# (可选) 白名单配置：填入允许交互的用户ID或群组/频道ID，多个ID用逗号分隔。不填则全员公开。
ALLOWED_CHAT_IDS=123456789,-100987654321
```

### 2. 一键启动

在配置文件所在目录运行：

```bash
docker compose up -d
```
*(默认拉取 latest 标签，始终保持最新。如需锁定版本，可在 compose.yaml 中将 image 修改为特定版本号，例如 yushum/douyin-tiktok-telegram-bot:1.0.0)*

## 🛠 高级：从源码构建

如果你希望自行修改代码或进行二次开发，可以使用以下命令在本地重新构建镜像：

```bash
# 请确保你的 compose.yaml 中的 tg-bot 服务开启了 `build: .` 并注释掉了 `image`
docker compose up -d --build
```

## 🙏 致谢

- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) - 提供底层数据解析核心爬虫支持。
- [ParseHub](https://github.com/z-mio/ParseHub) - 提供降级与多媒体解析支持。
