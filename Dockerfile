# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: install Python deps into an isolated venv (kept out of the final
# image's build tooling / pip caches).
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && find /opt/venv -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find /opt/venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ---------------------------------------------------------------------------
# Compress static ffmpeg/ffprobe with UPX (~4x smaller; same codecs).
# Source: mwader/static-ffmpeg (hardened static PIE, multi-arch).
# ---------------------------------------------------------------------------
FROM mwader/static-ffmpeg:9.0.1 AS ffmpeg-src
FROM alpine:3.21 AS ffmpeg

COPY --from=ffmpeg-src /ffmpeg /ffmpeg
COPY --from=ffmpeg-src /ffprobe /ffprobe
RUN apk add --no-cache upx \
    && upx --best --lzma /ffmpeg /ffprobe

# ---------------------------------------------------------------------------
# Runtime: slim Python + compressed static ffmpeg (no apt multimedia stack).
# apt ffmpeg alone was ~464 MB.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_FILE=/app/logs/bot.log \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app

RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid appgroup --shell /usr/sbin/nologin --create-home appuser \
    && mkdir -p /app/downloads /app/logs \
    && chown -R appuser:appgroup /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appgroup telegram_share_bot /app/telegram_share_bot

USER appuser

CMD ["python", "-m", "telegram_share_bot"]
