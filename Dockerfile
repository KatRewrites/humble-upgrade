# Discord bot runtime - Humble Upgrade (Railway)
FROM python:3.12-slim

WORKDIR /app

# /data is the Railway volume mount. DB + daily backups live there so they survive redeploys.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DATABASE_PATH=/data/bot.db \
    BACKUP_DIR=/data/db-backups

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py docker-entrypoint.sh ./
COPY cogs ./cogs
COPY services ./services
COPY assets ./assets

RUN chmod +x docker-entrypoint.sh && mkdir -p /data

# Entrypoint waits for /data/bot.db when WAIT_FOR_DB=1 (cutover), then runs bot.py.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "bot.py"]
