# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

# Prevent python from writing .pyc files and enable unbuffered streaming logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_FILE=/app/logs/bot.log

# Install ffmpeg (required by yt-dlp for media merging/muxing) and ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create a non-privileged user and setup runtime directories
RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser \
    && mkdir -p /app/downloads /app/logs \
    && chown -R appuser:appgroup /app

# Install dependencies in a separate layer for optimal Docker layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY sendmedia_bot /app/sendmedia_bot
RUN chown -R appuser:appgroup /app

USER appuser

# Run the bot module
CMD ["python", "-m", "sendmedia_bot"]
