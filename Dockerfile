FROM python:3.11-alpine

WORKDIR /app
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 强制禁用Python的标准输出缓冲，实时打印日志
ENV PYTHONUNBUFFERED=1

COPY bot.py .

CMD ["python", "bot.py"]
