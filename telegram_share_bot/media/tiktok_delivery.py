"""Finish TikTok photo posts as native video or audio within send limits."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from telegram_share_bot import strings
from telegram_share_bot.media.duration import ensure_full_media_duration
from telegram_share_bot.media.files import _cleanup_dir
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaFormat, MediaKind
from telegram_share_bot.media.transcode import _convert_audio_for_telegram, _optimize_video_file
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.tiktok.service import download_tiktok_slideshow

logger = logging.getLogger(__name__)


def try_tiktok_slideshow(
    url: str,
    work_dir: Path,
    *,
    source_limit: int,
    max_file_bytes: int,
    deadline: float,
    slide_ms: int,
    max_images: int,
    images_loop: bool,
    abort_event: threading.Event | None,
    https_only: bool,
    allowed_hosts: frozenset[str] | None,
    media_format: MediaFormat,
    max_media_duration_seconds: int,
    on_optimizing: Callable[[], None] | None,
) -> DownloadedMedia | None:
    """Return finished slideshow media, or None for a non-photo URL."""
    try:
        slideshow = download_tiktok_slideshow(
            url,
            work_dir,
            max_file_bytes=source_limit,
            timeout_seconds=max(1, int(deadline - time.monotonic())),
            slide_ms=slide_ms,
            max_images=max_images,
            images_loop=images_loop,
            abort_event=abort_event,
            https_only=https_only,
            allowed_hosts=allowed_hosts,
            media_format=media_format,
            max_media_duration_seconds=max_media_duration_seconds,
        )
        if slideshow is None:
            return None
        ensure_full_media_duration(slideshow.duration, max_media_duration_seconds)
        if media_format is MediaFormat.AUDIO:
            result_path = _convert_audio_for_telegram(
                slideshow.path,
                max_file_bytes=max_file_bytes,
                deadline=deadline,
                abort_event=abort_event,
            )
            result_kind = MediaKind.AUDIO
        else:
            result_path = _optimize_video_file(
                slideshow.path,
                max_file_bytes=max_file_bytes,
                deadline=deadline,
                abort_event=abort_event,
                on_optimizing=on_optimizing,
            )
            result_kind = slideshow.kind
        return DownloadedMedia(
            path=result_path,
            title=slideshow.title,
            kind=result_kind,
            duration=slideshow.duration,
        )
    except DownloadError:
        _cleanup_dir(work_dir)
        raise
    except Exception as exc:
        _cleanup_dir(work_dir)
        logger.warning("Slideshow path failed for %s: %s", safe_url_for_log(url), exc)
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc
