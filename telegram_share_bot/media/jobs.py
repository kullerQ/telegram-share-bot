"""Async download job orchestration and download directory cleanup."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
)
from telegram_share_bot.media.files import _cleanup_dir
from telegram_share_bot.media.models import (
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.security import (
    is_allowed_media_host,
    is_https_url,
    is_safe_media_url,
)
from telegram_share_bot.media.transfer import _download_sync

logger = logging.getLogger(__name__)


def _cleanup_finished_download(
    worker: asyncio.Task[DownloadedMedia], work_dir_holder: list[Path]
) -> None:
    """Clean an aborted worker's files only after its thread has stopped."""
    try:
        worker.result()
    except (asyncio.CancelledError, Exception):
        pass

    if work_dir_holder:
        try:
            _cleanup_dir(work_dir_holder[0])
        except OSError:
            logger.debug("Could not clean completed download directory", exc_info=True)


async def download_media(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
    allowed_hosts: frozenset[str] | None = None,
    *,
    https_only: bool = False,
    slideshow_slide_ms: int = 2500,
    slideshow_max_images: int = 35,
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    max_estimated_download_seconds: int = DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    on_optimizing: Callable[[], None] | None = None,
) -> DownloadedMedia:
    if not is_allowed_media_host(url, allowed_hosts):
        raise DownloadError(strings.DOWNLOAD_HOST_NOT_ALLOWED)
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    abort_event = threading.Event()
    work_dir_holder: list[Path] = []
    worker = asyncio.create_task(
        asyncio.to_thread(
            _download_sync,
            url,
            download_dir,
            max_file_bytes,
            timeout_seconds,
            abort_event,
            work_dir_holder,
            https_only=https_only,
            allowed_hosts=allowed_hosts,
            slideshow_slide_ms=slideshow_slide_ms,
            slideshow_max_images=slideshow_max_images,
            slideshow_images_loop=slideshow_images_loop,
            time_range=time_range,
            media_format=media_format,
            quality_policy=quality_policy,
            max_estimated_download_seconds=max_estimated_download_seconds,
            max_media_duration_seconds=max_media_duration_seconds,
            on_optimizing=on_optimizing,
        )
    )
    try:
        completed, _ = await asyncio.wait({worker}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        abort_event.set()
        worker.add_done_callback(lambda task: _cleanup_finished_download(task, work_dir_holder))
        raise

    if worker not in completed:
        abort_event.set()
        worker.add_done_callback(lambda task: _cleanup_finished_download(task, work_dir_holder))
        raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds))
    return worker.result()


def cleanup_media(media: DownloadedMedia) -> None:
    path = media.path
    parent = path.parent
    path.unlink(missing_ok=True)
    if parent.name and parent != path:
        _cleanup_dir(parent)


def cleanup_stale_downloads(
    download_dir: Path,
    max_age_seconds: int = 3600,
) -> int:
    """Remove abandoned download subdirectories older than max_age_seconds.

    Returns the number of directories removed. Top-level files (e.g. the cache DB)
    are left untouched.
    """
    if not download_dir.exists() or not download_dir.is_dir():
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for child in download_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        logger.info("Removing stale download directory: %s", child)
        _cleanup_dir(child)
        removed += 1
    return removed
