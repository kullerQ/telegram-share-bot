"""TikTok photo-post URL recognition and slideshow metadata extraction."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.networking.impersonate import ImpersonateTarget

from telegram_share_bot import strings
from telegram_share_bot.config import DEFAULT_SLIDESHOW_MAX_IMAGES
from telegram_share_bot.media.models import DownloadError
from telegram_share_bot.media.security import is_safe_media_url
from telegram_share_bot.platforms.urls import safe_url_for_log

logger = logging.getLogger(__name__)

_TIKTOK_PHOTO_RE = re.compile(r"^/(@[\w.\-]+)/photo/(\d+)")
_TIKTOK_SHORT_HOSTS = frozenset({"vt.tiktok.com", "vm.tiktok.com"})
_TIKTOK_HOST_SUFFIXES = frozenset({"tiktok.com"})

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


@dataclass(frozen=True, slots=True)
class TikTokPhotoRef:
    user: str
    video_id: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class SlideshowSource:
    image_urls: tuple[str, ...]
    audio_url: str | None
    title: str
    canonical_url: str
    audio_duration: float | None = None


# Memoize short-link resolutions so get_direct_stream + download_media share one lookup.
_SHORT_LINK_TTL_SECONDS = 300.0
_short_link_cache: dict[str, tuple[float, TikTokPhotoRef | None]] = {}
_short_link_lock = threading.Lock()


def _is_tiktok_host(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if not host:
        return False
    if host in _TIKTOK_SHORT_HOSTS:
        return True
    for suffix in _TIKTOK_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _photo_ref_from_url(url: str) -> TikTokPhotoRef | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    hostname = parsed.hostname
    if not hostname or not _is_tiktok_host(hostname):
        return None
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if host in _TIKTOK_SHORT_HOSTS:
        return None
    match = _TIKTOK_PHOTO_RE.match(parsed.path or "")
    if match is None:
        return None
    user, video_id = match.group(1), match.group(2)
    return TikTokPhotoRef(
        user=user,
        video_id=video_id,
        canonical_url=f"https://www.tiktok.com/{user}/photo/{video_id}",
    )


def _resolve_short_link(url: str) -> TikTokPhotoRef | None:
    """Follow a vt/vm.tiktok.com redirect and return a photo ref if applicable."""
    now = time.monotonic()
    with _short_link_lock:
        cached = _short_link_cache.get(url)
        if cached is not None:
            expires_at, ref = cached
            if now < expires_at:
                return ref
            _short_link_cache.pop(url, None)

    resolved: TikTokPhotoRef | None = None
    try:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            # Prefer HEAD; fall back to GET if the CDN rejects HEAD.
            final_url: str | None = None
            for method in ("HEAD", "GET"):
                try:
                    request = Request(
                        url,
                        method=method,
                        extensions={"impersonate": ImpersonateTarget("chrome")},
                    )
                    response = ydl.urlopen(request)  # type: ignore[arg-type]
                    final_url = getattr(response, "url", None) or url
                    # Drain/close to free the connection.
                    with response:
                        if method == "GET":
                            _ = response.read(64)
                    break
                except RequestError:
                    continue
            if final_url:
                resolved = _photo_ref_from_url(final_url)
    except Exception as exc:
        logger.debug(
            "TikTok short-link resolve failed for %s: %s",
            safe_url_for_log(url),
            exc,
        )
        resolved = None

    with _short_link_lock:
        _short_link_cache[url] = (now + _SHORT_LINK_TTL_SECONDS, resolved)
        # Bound cache size.
        if len(_short_link_cache) > 256:
            oldest_key = next(iter(_short_link_cache))
            _short_link_cache.pop(oldest_key, None)
    return resolved


def detect_tiktok_photo_post(url: str) -> TikTokPhotoRef | None:
    """Return a photo-post ref for TikTok /photo/ URLs (and resolved short links).

    Canonical ``tiktok.com/@user/photo/<id>`` URLs need no network. Short hosts
    (``vt.tiktok.com`` / ``vm.tiktok.com``) are resolved once and memoized.
    """
    stripped = url.strip()
    if not stripped:
        return None
    direct = _photo_ref_from_url(stripped)
    if direct is not None:
        return direct
    try:
        hostname = urlsplit(stripped).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if host not in _TIKTOK_SHORT_HOSTS:
        return None
    return _resolve_short_link(stripped)


def clear_short_link_cache() -> None:
    """Test helper: drop memoized short-link resolutions."""
    with _short_link_lock:
        _short_link_cache.clear()


def _pick_image_url(image_entry: Any) -> str | None:
    if not isinstance(image_entry, dict):
        return None
    image_url = image_entry.get("imageURL")
    if not isinstance(image_url, dict):
        return None
    url_list = image_url.get("urlList")
    if not isinstance(url_list, list):
        return None
    for candidate in url_list:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


def extract_slideshow(
    ref: TikTokPhotoRef,
    *,
    max_images: int = DEFAULT_SLIDESHOW_MAX_IMAGES,
    https_only: bool = False,
    socket_timeout: float = 30,
) -> SlideshowSource:
    """Extract image URLs and music from a TikTok photo post.

    Uses yt-dlp's TikTok extractor internals (``_extract_web_data_and_status``)
    because the public extract path discards ``imagePost`` slides.
    """
    try:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": socket_timeout,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ie = ydl.get_info_extractor("TikTok")
            ie.initialize()
            # TikTokIE internals: public extract discards imagePost slides.
            web_url = ie._create_url(  # type: ignore[attr-defined]
                ref.user.lstrip("@"), ref.video_id
            )
            item, status = ie._extract_web_data_and_status(  # type: ignore[attr-defined]
                web_url, ref.video_id, fatal=False
            )
    except DownloadError:
        raise
    except Exception as exc:
        logger.warning(
            "TikTok slideshow extract failed for %s: %s",
            ref.canonical_url,
            exc,
        )
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED) from exc

    if not isinstance(item, dict) or status not in (0, None):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)

    image_post = item.get("imagePost")
    if not isinstance(image_post, dict):
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)
    raw_images = image_post.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    image_urls: list[str] = []
    for entry in raw_images:
        if len(image_urls) >= max_images:
            break
        url = _pick_image_url(entry)
        if url is None:
            continue
        # CDN hosts are not on ALLOWED_MEDIA_HOSTS; only SSRF-check them.
        if not is_safe_media_url(url, https_only=https_only):
            logger.warning(
                "Skipping unsafe slideshow image URL: %s", safe_url_for_log(url)
            )
            continue
        image_urls.append(url)

    if not image_urls:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    audio_url: str | None = None
    audio_duration: float | None = None
    music = item.get("music")
    if isinstance(music, dict):
        play_url = music.get("playUrl")
        if isinstance(play_url, str) and play_url.startswith("http"):
            if is_safe_media_url(play_url, https_only=https_only):
                audio_url = play_url
            else:
                logger.warning(
                    "Ignoring unsafe slideshow audio URL: %s",
                    safe_url_for_log(play_url),
                )
        dur_raw = music.get("duration")
        if isinstance(dur_raw, (int, float)) and float(dur_raw) > 0:
            audio_duration = float(dur_raw)

    title_raw = item.get("desc")
    title = str(title_raw or f"tiktok-{ref.video_id}")[:64]

    return SlideshowSource(
        image_urls=tuple(image_urls),
        audio_url=audio_url,
        title=title,
        canonical_url=ref.canonical_url,
        audio_duration=audio_duration,
    )
