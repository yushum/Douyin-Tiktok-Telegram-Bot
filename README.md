# Douyin/TikTok Telegram Bot

![Docker Pulls](https://img.shields.io/docker/pulls/yushum/douyin-tiktok-telegram-bot?style=flat-square)
![Docker Image Size (tag)](https://img.shields.io/docker/image-size/yushum/douyin-tiktok-telegram-bot/latest?style=flat-square)
![GitHub License](https://img.shields.io/github/license/yushum/Douyin-Tiktok-Telegram-Bot?style=flat-square)
![Multi-Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?style=flat-square)
![Docker Image Version (latest semver)](https://img.shields.io/docker/v/yushum/douyin-tiktok-telegram-bot?sort=semver&style=flat-square)

这是一个基于 Python `aiogram` 框架构建的高性能 Telegram 机器人，专门用于将抖音和 TikTok 的分享链接解析为无水印视频或图集，并在 Telegram 中原生发送。

## ✨ 核心特性

- **超清无损**：自动探测并拉取抖音底层 CDN 提供的最高分辨率源文件 (最高支持 4K)。
- **实况与图集支持**：完美解析抖音图集和实况照片，以无损图片流 (MediaGroup) 形式发送。
- **2GB 大文件突破**：基于双容器共享数据卷架构打通 Telegram Local API Server，彻底告别官方 50MB 传输限制，最高支持 2GB 的长视频极速直传。
- **防盗链穿透**：通过内存缓冲与伪装请求，有效绕过抖音图集和视频封面的 `403 Forbidden` 严格防盗链机制。
- **全场景适配**：私聊即时响应；支持频道静默发布与带链接原帖自动清理；群组模式下智能嗅探短链接，实现纯净防打扰。
- **高可用自动降级**：内置 ParseHub 降级方案，当原 API 失效（例如遇到不支持的“抖音日常”格式）时无缝切换至降级解析。
- **安全防滥用**：内置多维白名单机制，支持精细化配置授权的用户、群组或频道。

## 📦 快速部署

### 1. 准备配置

创建一个目录用于存放数据和配置，并在目录内创建 `compose.yaml` 文件（直接复制本仓库的 `compose.yaml`），然后创建 `.env` 文件填入凭证：

```env
# 去 https://my.telegram.org 申请
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890

# 去 @BotFather 申请
BOT_TOKEN=123456789:ABCdefGHIjklmNOPQrstUVwxyZ

# 默认使用开源演示节点，为保证稳定性，强烈建议自行部署该 API
API_BASE_URL=https://douyin.wtf

# (可选) 当原 API 解析失败（如不支持的日常视频）时，会调用 ParseHub 降级解析
# 某些抖音链接需要登录态才能解析，如果你遇到了降级解析也失败的情况，请在此处填入你的抖音网页版 Cookie
DOUYIN_COOKIE=

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

如果你希望自行修改代码或进行二次开发，可以使用以下命令在本地重新构建镜像（无需拉取官方镜像）：

```bash
# 请确保你的 compose.yaml 中的 tg-bot 服务开启了 `build: .` 并注释掉了 `image`
docker compose up -d --build
```

## 🙏 致谢

- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) - 提供底层数据解析核心 API 支持。