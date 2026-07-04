# Douyin/TikTok Telegram Bot

这是一个基于 Python `aiogram` 框架构建的高性能 Telegram 机器人，专门用于将抖音和 TikTok 的分享链接解析为无水印视频或图集，并在 Telegram 中原生发送。

## ✨ 核心特性

- **超清无损**：自动探测并拉取抖音底层 CDN 提供的最高分辨率源文件 (最高支持 4K)。
- **实况与图集支持**：完美解析抖音图集和实况照片，以无损图片流 (MediaGroup) 形式发送。
- **2GB 大文件突破**：基于 Telegram Local Bot API Server 架构，彻底告别官方 50MB 传输限制，最高支持 2000MB 的长视频极速转发。
- **防盗链穿透**：通过内存缓冲与伪装请求，有效绕过抖音图集和视频封面的 `403 Forbidden` 防盗链机制。
- **全场景适配**：私聊直接响应；支持频道 (Channel) 静默发布与链接自动清理；群组模式下静默嗅探短链接，防止打扰。
- **秒级流播**：发出的视频原生支持边下边播，且附带提取到的高清封面图。

## 📦 快速部署

本项目使用 Docker Compose 进行一键部署，自带 2GB 传输解锁环境。

### 1. 准备配置文件

在项目根目录下创建一个 `.env` 文件，填入你的开发者凭证：

```env
# 去 https://my.telegram.org 申请
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890

# 去 @BotFather 申请
BOT_TOKEN=123456789:ABCdefGHIjklmNOPQrstUVwxyZ
```

### 2. 一键启动

```bash
docker compose up -d --build
```

启动后，向你的 Bot 发送一段抖音分享口令即可体验！

## 🙏 致谢 (Acknowledgements)

本项目站在巨人的肩膀上，特别感谢：

- [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) - 提供稳定、高性能的抖音/TikTok 数据解析核心 API 支持。
- **Google Gemini** - 在本项目的架构设计、长视频传输突破、防盗链降级策略以及异步代码重构方面提供了全流程的结对编程指导与技术支持。