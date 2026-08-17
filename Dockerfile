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

# 安装运行时系统动态链接库依赖 (liblz4, libjemalloc2 与 CA 根证书)
RUN apt-get update && \
    apt-get install -y --no-install-recommends liblz4-1 libjemalloc2 ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    find /usr/lib -name "libjemalloc.so.2" -exec ln -sf {} /usr/local/lib/libjemalloc.so.2 \;

# 从 builder 复制已编译安装的 Python 依赖库
COPY --from=builder /install /usr/local

# 从 builder 复制上游爬虫核心
COPY --from=builder /build/upstream_api /app/upstream_api

# 复制 Bot 源代码
COPY *.py .

# 环境变量设置：启用 jemalloc 高效内存分配与后台脏页回收
ENV PYTHONUNBUFFERED=1
ENV UPSTREAM_API_PATH=/app/upstream_api
ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so.2
ENV MALLOC_CONF="background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:2000"

CMD ["python", "bot.py"]
