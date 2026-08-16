FROM python:3.11-slim

WORKDIR /app

# 安装 git, ca-certificates 和基础构建依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# 拉取 Evil0ctal 上游最新解析器核心
RUN git clone --depth 1 https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git /app/upstream_api

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt -r /app/upstream_api/requirements.txt

# 环境变量设置
ENV PYTHONUNBUFFERED=1
ENV UPSTREAM_API_PATH=/app/upstream_api

COPY *.py .

CMD ["python", "bot.py"]
