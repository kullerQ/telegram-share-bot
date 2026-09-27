"""Configuration constants and immutable settings model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _ROOT / ".env"

# Stay under Telegram Bot API's ~50 MB upload limit.
TELEGRAM_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 45 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120
DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS = 45
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 180
DEFAULT_DELETE_STORAGE_MESSAGES = True
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 6
DEFAULT_MAX_DOWNLOADS_PER_USER = 3
DEFAULT_MAX_DOWNLOADS_PER_MINUTE = 10
DEFAULT_MAX_MEDIA_DURATION_SECONDS = 30 * 60
DEFAULT_DOWNLOAD_COOLDOWN_SECONDS = 2
DEFAULT_ALLOW_PUBLIC = False
DEFAULT_ALLOW_SHARED_STORAGE = False
DEFAULT_HTTPS_ONLY = True
DEFAULT_PLATFORM_LOGO_BASE_URL = (
    "https://raw.githubusercontent.com/kullerQ/telegram-share-bot/main/"
    "telegram_share_bot/assets/platforms"
)
DEFAULT_ALLOWED_MEDIA_HOSTS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "x.com",
        "twitter.com",
        "vxtwitter.com",
        "fxtwitter.com",
        "fixupx.com",
        "instagram.com",
        "tiktok.com",
        "reddit.com",
        "redd.it",
        "v.redd.it",
        "facebook.com",
        "fb.watch",
    }
)


class CaptionMode(str, Enum):
    """How captions are attached to sent media.

    ``media`` — use the extracted media title (previous default).
    ``custom`` — only a user-supplied caption after the URL; none if omitted.
    ``off`` — never attach a caption.
    """

    MEDIA = "media"
    CUSTOM = "custom"
    OFF = "off"


DEFAULT_CAPTION_MODE = CaptionMode.MEDIA
# Telegram Bot API caption limit for most media types.
TELEGRAM_CAPTION_MAX_LENGTH = 1024
DEFAULT_SLIDESHOW_SLIDE_MS = 2500
DEFAULT_SLIDESHOW_MAX_IMAGES = 35
DEFAULT_SLIDESHOW_IMAGES_LOOP = True


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    storage_chat_id: int
    max_file_bytes: int
    download_timeout_seconds: int
    download_dir: Path
    cache_db_path: Path
    delete_storage_messages: bool
    user_settings_db_path: Path | None = None
    upload_timeout_seconds: int = DEFAULT_UPLOAD_TIMEOUT_SECONDS
    max_estimated_download_seconds: int = DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    max_downloads_per_user: int = DEFAULT_MAX_DOWNLOADS_PER_USER
    max_downloads_per_minute: int = DEFAULT_MAX_DOWNLOADS_PER_MINUTE
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS
    download_cooldown_seconds: int = DEFAULT_DOWNLOAD_COOLDOWN_SECONDS
    allowed_user_ids: frozenset[int] = frozenset()
    allow_public: bool = DEFAULT_ALLOW_PUBLIC
    allow_shared_storage: bool = DEFAULT_ALLOW_SHARED_STORAGE
    https_only: bool = DEFAULT_HTTPS_ONLY
    # None means allow any host (`ALLOWED_MEDIA_HOSTS=*`).
    allowed_media_hosts: frozenset[str] | None = DEFAULT_ALLOWED_MEDIA_HOSTS
    caption_mode: CaptionMode = DEFAULT_CAPTION_MODE
    platform_logo_base_url: str | None = DEFAULT_PLATFORM_LOGO_BASE_URL
    slideshow_slide_ms: int = DEFAULT_SLIDESHOW_SLIDE_MS
    slideshow_max_images: int = DEFAULT_SLIDESHOW_MAX_IMAGES
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP

