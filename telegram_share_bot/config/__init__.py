"""Runtime configuration loaded from environment variables.

The package preserves the original ``telegram_share_bot.config`` import surface.
"""

from __future__ import annotations

from dotenv import load_dotenv

from telegram_share_bot.config.loader import load_settings
from telegram_share_bot.config.models import (
    _ENV_PATH,
    DEFAULT_ALLOW_PUBLIC,
    DEFAULT_ALLOW_SHARED_STORAGE,
    DEFAULT_ALLOWED_MEDIA_HOSTS,
    DEFAULT_CAPTION_MODE,
    DEFAULT_DELETE_STORAGE_MESSAGES,
    DEFAULT_DOWNLOAD_COOLDOWN_SECONDS,
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_HTTPS_ONLY,
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_MAX_DOWNLOADS_PER_MINUTE,
    DEFAULT_MAX_DOWNLOADS_PER_USER,
    DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_PLATFORM_LOGO_BASE_URL,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
    DEFAULT_SLIDESHOW_MAX_IMAGES,
    DEFAULT_SLIDESHOW_SLIDE_MS,
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    TELEGRAM_CAPTION_MAX_LENGTH,
    TELEGRAM_MAX_FILE_BYTES,
    CaptionMode,
    Settings,
)
from telegram_share_bot.config.models import (
    _ROOT as _ROOT,
)
from telegram_share_bot.config.parsing import (
    parse_bool,
    parse_caption_mode,
    parse_int,
    parse_media_hosts,
    parse_path,
    parse_user_ids,
)

load_dotenv(_ENV_PATH)

__all__ = [
    "DEFAULT_ALLOWED_MEDIA_HOSTS",
    "DEFAULT_ALLOW_PUBLIC",
    "DEFAULT_ALLOW_SHARED_STORAGE",
    "DEFAULT_CAPTION_MODE",
    "DEFAULT_DELETE_STORAGE_MESSAGES",
    "DEFAULT_DOWNLOAD_COOLDOWN_SECONDS",
    "DEFAULT_DOWNLOAD_TIMEOUT_SECONDS",
    "DEFAULT_HTTPS_ONLY",
    "DEFAULT_MAX_CONCURRENT_DOWNLOADS",
    "DEFAULT_MAX_DOWNLOADS_PER_MINUTE",
    "DEFAULT_MAX_DOWNLOADS_PER_USER",
    "DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_MEDIA_DURATION_SECONDS",
    "DEFAULT_PLATFORM_LOGO_BASE_URL",
    "DEFAULT_SLIDESHOW_IMAGES_LOOP",
    "DEFAULT_SLIDESHOW_MAX_IMAGES",
    "DEFAULT_SLIDESHOW_SLIDE_MS",
    "DEFAULT_UPLOAD_TIMEOUT_SECONDS",
    "TELEGRAM_CAPTION_MAX_LENGTH",
    "TELEGRAM_MAX_FILE_BYTES",
    "CaptionMode",
    "Settings",
    "load_settings",
    "parse_bool",
    "parse_caption_mode",
    "parse_int",
    "parse_media_hosts",
    "parse_path",
    "parse_user_ids",
]
