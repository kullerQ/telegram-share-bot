"""High-level TikTok slideshow download service."""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Any

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
    DEFAULT_SLIDESHOW_MAX_IMAGES,
    DEFAULT_SLIDESHOW_SLIDE_MS,
)
from telegram_share_bot.media.duration import ensure_full_media_duration
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaFormat, MediaKind
from telegram_share_bot.media.network import create_youtube_dl
from telegram_share_bot.media.security import _safe_dns_resolution
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.tiktok.assets import _download_bytes, _guess_ext, _probe_media_duration
from telegram_share_bot.tiktok.render import build_slideshow_video
from telegram_share_bot.tiktok.source import detect_tiktok_photo_post, extract_slideshow

logger = logging.getLogger(__name__)


def download_tiktok_slideshow(
    url: str,
    work_dir: Path,
    *,
    max_file_bytes: int,
    timeout_seconds: int,
    slide_ms: int = DEFAULT_SLIDESHOW_SLIDE_MS,
    max_images: int = DEFAULT_SLIDESHOW_MAX_IMAGES,
    images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    abort_event: threading.Event | None = None,
    https_only: bool = False,
    allowed_hosts: frozenset[str] | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
) -> DownloadedMedia | None:
    """If *url* is a TikTok photo post, build and return a slideshow video.

    Returns ``None`` when the URL is not a photo post (caller should fall through
    to the normal yt-dlp path).
    """
    _ = allowed_hosts  # user URL already checked by caller
    ref = detect_tiktok_photo_post(url)
    if ref is None:
        return None
    source = extract_slideshow(
        ref,
        max_images=max_images,
        https_only=https_only,
    )
    if source.audio_url is not None and (
        media_format is MediaFormat.AUDIO or images_loop or len(source.image_urls) == 1
    ):
        ensure_full_media_duration(
            source.audio_duration, max_media_duration_seconds
        )
    if media_format is MediaFormat.AUDIO:
        if source.audio_url is None:
            raise DownloadError(strings.AUDIO_UNAVAILABLE)
        if https_only and not source.audio_url.lower().startswith("https://"):
            raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
        audio_path = work_dir / f"slideshow-audio.{_guess_ext(source.audio_url, 'm4a')}"
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": min(30, timeout_seconds),
        }
        try:
            with _safe_dns_resolution():
                with create_youtube_dl(ydl_opts) as ydl:
                    _download_bytes(
                        ydl,
                        source.audio_url,
                        audio_path,
                        remaining_budget=max_file_bytes,
                        abort_event=abort_event,
                    )
        except DownloadError:
            raise
        except Exception as exc:
            logger.warning(
                "TikTok slideshow soundtrack download failed for %s: %s",
                safe_url_for_log(source.canonical_url),
                exc,
            )
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc
        raw_duration = (
            source.audio_duration
            if source.audio_duration is not None
            else _probe_media_duration(audio_path)
        )
        duration = (
            max(1, math.floor(raw_duration + 0.5))
            if raw_duration is not None
            else None
        )
        ensure_full_media_duration(duration, max_media_duration_seconds)
        return DownloadedMedia(
            path=audio_path,
            title=source.title,
            kind=MediaKind.AUDIO,
            duration=duration,
        )
    return build_slideshow_video(
        source,
        work_dir,
        max_file_bytes=max_file_bytes,
        timeout_seconds=timeout_seconds,
        slide_ms=slide_ms,
        images_loop=images_loop,
        abort_event=abort_event,
        https_only=https_only,
        max_media_duration_seconds=max_media_duration_seconds,
    )
