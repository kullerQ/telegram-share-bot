"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from dotenv import load_dotenv

from telegram_share_bot import strings

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _ROOT / ".env"
load_dotenv(_ENV_PATH)

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

_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off"})

T = TypeVar("T")


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


def parse_caption_mode(
    param_name: str,
    raw_value: str | None,
    default: CaptionMode = DEFAULT_CAPTION_MODE,
    env_file: Path = _ENV_PATH,
) -> CaptionMode:
    """Parse CAPTION_MODE: media | custom | off."""
    if raw_value is None or not raw_value.strip():
        return default
    stripped = raw_value.strip().lower()
    try:
        return CaptionMode(stripped)
    except ValueError:
        allowed = ", ".join(mode.value for mode in CaptionMode)
        reset = _handle_invalid_param(
            param_name=param_name,
            raw_value=raw_value,
            default_value=default.value,
            reason=f"Expected one of: {allowed}",
            env_file=env_file,
        )
        return CaptionMode(reset)


def parse_media_hosts(
    param_name: str,
    raw_value: str | None,
) -> frozenset[str] | None:
    """Parse ALLOWED_MEDIA_HOSTS.

    - unset/empty → default platform allowlist
    - ``*`` → allow any host (None)
    - comma-separated host suffixes otherwise
    """
    _ = param_name
    if raw_value is None or not raw_value.strip():
        return DEFAULT_ALLOWED_MEDIA_HOSTS
    stripped = raw_value.strip()
    if stripped == "*":
        return None
    hosts: set[str] = set()
    for part in stripped.split(","):
        host = part.strip().lower().removeprefix("www.").rstrip(".")
        if not host or "/" in host or "://" in host:
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{stripped}': "
                "Expected comma-separated hostnames or '*'."
            )
        hosts.add(host)
    if not hosts:
        return DEFAULT_ALLOWED_MEDIA_HOSTS
    return frozenset(hosts)


def parse_user_ids(
    param_name: str,
    raw_value: str | None,
    env_file: Path = _ENV_PATH,
) -> frozenset[int]:
    """Parse a comma-separated list of Telegram user ids.

    Empty / unset returns an empty set. Access still requires ALLOW_PUBLIC=true
    or a non-empty allowlist (see load_settings).
    """
    _ = env_file
    if raw_value is None or not raw_value.strip():
        return frozenset()

    stripped = raw_value.strip()
    ids: set[int] = set()
    for part in stripped.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError as exc:
            # Never reset to empty (that would open the bot). Fail closed.
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{stripped}': "
                "Expected a comma-separated list of integers. "
                "Please fix it in your .env file."
            ) from exc
    return frozenset(ids)


def _update_env_file(env_file: Path, key: str, new_value: str) -> None:
    try:
        if not env_file.exists():
            return
        lines = env_file.read_text(encoding="utf-8").splitlines()
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        updated = False
        new_lines: list[str] = []
        for line in lines:
            if pattern.match(line):
                new_lines.append(f"{key}={new_value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={new_value}")
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        logger.info("Updated %s=%s in %s", key, new_value, env_file.name)
    except Exception as exc:
        logger.warning("Could not update %s in %s: %s", key, env_file, exc)


def _handle_invalid_param(
    param_name: str,
    raw_value: str,
    default_value: T,
    reason: str,
    env_file: Path = _ENV_PATH,
) -> T:
    msg = f"Invalid configuration for {param_name}='{raw_value}': {reason}"
    logger.warning(msg)

    # In a non-interactive environment (CI, Docker, background service), fail fast.
    if not (sys.stdin and sys.stdin.isatty()):
        raise RuntimeError(
            f"{msg}. Non-interactive environment: please fix {param_name} in your .env file."
        )

    print(f"\n[WARNING] {msg}", file=sys.stderr)
    try:
        prompt = (
            f"Would you like to reset {param_name} to its default value '{default_value}'? [y/N]: "
        )
        choice = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError(f"Configuration setup aborted for {param_name}.") from exc

    if choice in ("y", "yes"):
        logger.info("Resetting %s to default value: %s", param_name, default_value)
        _update_env_file(env_file, param_name, str(default_value).lower())
        return default_value

    raise RuntimeError(
        f"Invalid configuration for {param_name}='{raw_value}'. Please fix it in your .env file."
    )


def parse_bool(
    param_name: str,
    raw_value: str | None,
    default: bool = DEFAULT_DELETE_STORAGE_MESSAGES,
    env_file: Path = _ENV_PATH,
) -> bool:
    if raw_value is None or not raw_value.strip():
        return default
    val = raw_value.strip().lower()
    if val in _TRUE_VALUES:
        return True
    if val in _FALSE_VALUES:
        return False
    return _handle_invalid_param(
        param_name=param_name,
        raw_value=raw_value,
        default_value=default,
        reason="Expected a boolean (true, false, 1, 0, yes, no)",
        env_file=env_file,
    )


def parse_int(
    param_name: str,
    raw_value: str | None,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
    env_file: Path = _ENV_PATH,
) -> int:
    if raw_value is None or not raw_value.strip():
        return default
    stripped = raw_value.strip()
    try:
        val = int(stripped)
        if min_value is not None and val < min_value:
            raise ValueError(f"must be >= {min_value}")
        if max_value is not None and val > max_value:
            raise ValueError(f"must be <= {max_value}")
        return val
    except ValueError as exc:
        bounds = []
        if min_value is not None:
            bounds.append(f">= {min_value}")
        if max_value is not None:
            bounds.append(f"<= {max_value}")
        bounds_info = f" ({', '.join(bounds)})" if bounds else ""
        return _handle_invalid_param(
            param_name=param_name,
            raw_value=stripped,
            default_value=default,
            reason=f"Expected an integer{bounds_info}: {exc}",
            env_file=env_file,
        )


def parse_path(
    param_name: str,
    raw_value: str | None,
    default: Path,
    env_file: Path = _ENV_PATH,
    *,
    must_be_under: Path | None = None,
) -> Path:
    if raw_value is None or not raw_value.strip():
        path = default
    else:
        stripped = raw_value.strip()
        try:
            path = Path(stripped)
            if path.exists() and path.is_dir():
                raise ValueError(f"Path '{path}' is a directory, expected a file path")
        except Exception as exc:
            return _handle_invalid_param(
                param_name=param_name,
                raw_value=stripped,
                default_value=default,
                reason=str(exc),
                env_file=env_file,
            )

    if must_be_under is not None:
        try:
            path.resolve().relative_to(must_be_under.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{path}': "
                f"path must be under {must_be_under}"
            ) from exc
    return path


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
