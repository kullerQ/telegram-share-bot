#!/bin/sh
set -eu

image_ref="${1:?usage: smoke_docker_image.sh IMAGE_REF}"

docker run --rm \
    --env BOT_TOKEN=123456789:codex-image-smoke \
    --env STORAGE_CHAT_ID=-1001234567890 \
    --env ALLOW_PUBLIC=true \
    --env LOG_FILE=/app/logs/bot.log \
    --entrypoint /bin/sh \
    "$image_ref" \
    -eu -c '
        test "$(id -u)" -ne 0

        for directory in /app/downloads /app/data /app/logs; do
            test -d "$directory"
            test -w "$directory"
            marker="$directory/.image-smoke-$$"
            : > "$marker"
            rm "$marker"
        done

        python -c "from telegram_share_bot.logging_filters import configure_logging; configure_logging(); from telegram_share_bot.app import build_application; build_application()"
        test -f /app/downloads/media_cache.db
        test -f /app/data/user_settings.db
        test -f /app/logs/bot.log
        ffmpeg -hide_banner -version >/dev/null
    '
