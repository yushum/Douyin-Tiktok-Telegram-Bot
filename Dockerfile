# ==========================================
# Stage 1: Build & Dependencies Extraction
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装编译依赖与 Git
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates gcc g++ python3-dev liblz4-dev && \
    rm -rf /var/lib/apt/lists/*

# 支持传入 upstream commit hash 以主动破坏构建缓存并拉取最新上游爬虫
ARG UPSTREAM_COMMIT=""

# 拉取 Evil0ctal 上游仓库并仅提取 crawlers 解析核心模块
RUN git clone --depth 1 https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git /tmp/upstream_repo && \
    mkdir -p /build/upstream_api && \
    cp -r /tmp/upstream_repo/crawlers /build/upstream_api/crawlers && \
    rm -rf /tmp/upstream_repo

COPY requirements.txt .

# 安装精简依赖到独立前缀目录，避免冗余构建缓存
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==========================================
# Stage 2: Final Minimal Runtime Image
# ==========================================
FROM python:3.12-slim

WORKDIR /app

# 安装运行时系统动态链接库依赖 (liblz4 与 CA 根证书)
RUN apt-get update && \
    apt-get install -y --no-install-recommends liblz4-1 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 复制已编译安装的 Python 依赖库
COPY --from=builder /install /usr/local

# 从 builder 复制上游爬虫核心
COPY --from=builder /build/upstream_api /app/upstream_api

# 复制 Bot 源代码
COPY *.py .

# 环境变量设置
ENV PYTHONUNBUFFERED=1
ENV UPSTREAM_API_PATH=/app/upstream_api

CMD ["python", "bot.py"]
