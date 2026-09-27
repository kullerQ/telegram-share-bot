"""Load and validate the complete runtime settings object."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from telegram_share_bot import strings
from telegram_share_bot.config.models import (
    _ENV_PATH,
    _ROOT,
    DEFAULT_ALLOW_PUBLIC,
    DEFAULT_ALLOW_SHARED_STORAGE,
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
    TELEGRAM_MAX_FILE_BYTES,
    Settings,
)
from telegram_share_bot.config.parsing import (
    parse_bool,
    parse_caption_mode,
    parse_int,
    parse_media_hosts,
    parse_path,
    parse_user_ids,
)


def load_settings(env_file: Path = _ENV_PATH) -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token.startswith("123456:"):
        raise RuntimeError(strings.CONFIG_MISSING_BOT_TOKEN)

    storage_raw = os.getenv("STORAGE_CHAT_ID", "").strip()
    if not storage_raw or storage_raw == "123456789":
        raise RuntimeError(strings.CONFIG_MISSING_STORAGE_CHAT_ID)

    try:
        storage_chat_id = int(storage_raw)
    except ValueError as exc:
        raise RuntimeError(strings.CONFIG_STORAGE_CHAT_ID_NOT_INT) from exc

    max_file_bytes = parse_int(
        "MAX_FILE_BYTES",
        os.getenv("MAX_FILE_BYTES"),
        default=DEFAULT_MAX_FILE_BYTES,
        min_value=1024,
        max_value=TELEGRAM_MAX_FILE_BYTES,
        env_file=env_file,
    )

    download_timeout = parse_int(
        "DOWNLOAD_TIMEOUT_SECONDS",
        os.getenv("DOWNLOAD_TIMEOUT_SECONDS"),
        default=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        min_value=1,
        env_file=env_file,
    )

    max_estimated_download_seconds = parse_int(
        "MAX_ESTIMATED_DOWNLOAD_SECONDS",
        os.getenv("MAX_ESTIMATED_DOWNLOAD_SECONDS"),
        default=DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
        min_value=0,
        env_file=env_file,
    )

    download_dir = _ROOT / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    data_dir = _ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    cache_db_path = parse_path(
        "CACHE_DB_PATH",
        os.getenv("CACHE_DB_PATH"),
        default=download_dir / "media_cache.db",
        env_file=env_file,
        must_be_under=_ROOT,
    )

    delete_storage_messages = parse_bool(
        "DELETE_STORAGE_MESSAGES",
        os.getenv("DELETE_STORAGE_MESSAGES"),
        default=DEFAULT_DELETE_STORAGE_MESSAGES,
        env_file=env_file,
    )

    upload_timeout = parse_int(
        "UPLOAD_TIMEOUT_SECONDS",
        os.getenv("UPLOAD_TIMEOUT_SECONDS"),
        default=DEFAULT_UPLOAD_TIMEOUT_SECONDS,
        min_value=1,
        env_file=env_file,
    )

    max_concurrent_downloads = parse_int(
        "MAX_CONCURRENT_DOWNLOADS",
        os.getenv("MAX_CONCURRENT_DOWNLOADS"),
        default=DEFAULT_MAX_CONCURRENT_DOWNLOADS,
        min_value=0,
        max_value=20,
        env_file=env_file,
    )

    max_downloads_per_user = parse_int(
        "MAX_DOWNLOADS_PER_USER",
        os.getenv("MAX_DOWNLOADS_PER_USER"),
        default=DEFAULT_MAX_DOWNLOADS_PER_USER,
        min_value=0,
        max_value=5,
        env_file=env_file,
    )

    max_downloads_per_minute = parse_int(
        "MAX_DOWNLOADS_PER_MINUTE",
        os.getenv("MAX_DOWNLOADS_PER_MINUTE"),
        default=DEFAULT_MAX_DOWNLOADS_PER_MINUTE,
        min_value=0,
        max_value=1000,
        env_file=env_file,
    )

    max_media_duration_seconds = parse_int(
        "MAX_MEDIA_DURATION_SECONDS",
        os.getenv("MAX_MEDIA_DURATION_SECONDS"),
        default=DEFAULT_MAX_MEDIA_DURATION_SECONDS,
        min_value=0,
        max_value=24 * 60 * 60,
        env_file=env_file,
    )

    download_cooldown_seconds = parse_int(
        "DOWNLOAD_COOLDOWN_SECONDS",
        os.getenv("DOWNLOAD_COOLDOWN_SECONDS"),
        default=DEFAULT_DOWNLOAD_COOLDOWN_SECONDS,
        min_value=0,
        max_value=60,
        env_file=env_file,
    )

    allowed_user_ids = parse_user_ids(
        "ALLOWED_USER_IDS",
        os.getenv("ALLOWED_USER_IDS"),
        env_file=env_file,
    )

    allow_public = parse_bool(
        "ALLOW_PUBLIC",
        os.getenv("ALLOW_PUBLIC"),
        default=DEFAULT_ALLOW_PUBLIC,
        env_file=env_file,
    )

    allow_shared_storage = parse_bool(
        "ALLOW_SHARED_STORAGE",
        os.getenv("ALLOW_SHARED_STORAGE"),
        default=DEFAULT_ALLOW_SHARED_STORAGE,
        env_file=env_file,
    )

    https_only = parse_bool(
        "HTTPS_ONLY",
        os.getenv("HTTPS_ONLY"),
        default=DEFAULT_HTTPS_ONLY,
        env_file=env_file,
    )

    if not allowed_user_ids and not allow_public:
        raise RuntimeError(strings.CONFIG_MISSING_ACCESS_CONTROL)

    allowed_media_hosts = parse_media_hosts(
        "ALLOWED_MEDIA_HOSTS",
        os.getenv("ALLOWED_MEDIA_HOSTS"),
    )

    caption_mode = parse_caption_mode(
        "CAPTION_MODE",
        os.getenv("CAPTION_MODE"),
        default=DEFAULT_CAPTION_MODE,
        env_file=env_file,
    )

    logo_base_raw = os.getenv("PLATFORM_LOGO_BASE_URL")
    platform_logo_base_url = (
        DEFAULT_PLATFORM_LOGO_BASE_URL
        if logo_base_raw is None
        else logo_base_raw.strip().rstrip("/") or None
    )
    if platform_logo_base_url is not None and platform_logo_base_url != "upstream":
        parsed_logo_url = urlsplit(platform_logo_base_url)
        if (
            parsed_logo_url.scheme != "https"
            or not parsed_logo_url.netloc
            or parsed_logo_url.query
            or parsed_logo_url.fragment
        ):
            raise RuntimeError(
                "PLATFORM_LOGO_BASE_URL must be 'upstream' or a public HTTPS directory URL."
            )

    slideshow_slide_ms = parse_int(
        "SLIDESHOW_SLIDE_MS",
        os.getenv("SLIDESHOW_SLIDE_MS"),
        default=DEFAULT_SLIDESHOW_SLIDE_MS,
        min_value=500,
        max_value=10000,
        env_file=env_file,
    )

    slideshow_max_images = parse_int(
        "SLIDESHOW_MAX_IMAGES",
        os.getenv("SLIDESHOW_MAX_IMAGES"),
        default=DEFAULT_SLIDESHOW_MAX_IMAGES,
        min_value=1,
        max_value=100,
        env_file=env_file,
    )

    slideshow_images_loop = parse_bool(
        "SLIDESHOW_IMAGES_LOOP",
        os.getenv("SLIDESHOW_IMAGES_LOOP"),
        default=DEFAULT_SLIDESHOW_IMAGES_LOOP,
        env_file=env_file,
    )

    return Settings(
        bot_token=token,
        storage_chat_id=storage_chat_id,
        max_file_bytes=max_file_bytes,
        download_timeout_seconds=download_timeout,
        max_estimated_download_seconds=max_estimated_download_seconds,
        download_dir=download_dir,
        cache_db_path=cache_db_path,
        delete_storage_messages=delete_storage_messages,
        user_settings_db_path=data_dir / "user_settings.db",
        upload_timeout_seconds=upload_timeout,
        max_concurrent_downloads=max_concurrent_downloads,
        max_downloads_per_user=max_downloads_per_user,
        max_downloads_per_minute=max_downloads_per_minute,
        max_media_duration_seconds=max_media_duration_seconds,
        download_cooldown_seconds=download_cooldown_seconds,
        allowed_user_ids=allowed_user_ids,
        allow_public=allow_public,
        allow_shared_storage=allow_shared_storage,
        https_only=https_only,
        allowed_media_hosts=allowed_media_hosts,
        caption_mode=caption_mode,
        platform_logo_base_url=platform_logo_base_url,
        slideshow_slide_ms=slideshow_slide_ms,
        slideshow_max_images=slideshow_max_images,
        slideshow_images_loop=slideshow_images_loop,
    )
