"""Async download job orchestration and download directory cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

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
from telegram_share_bot.media.transfer import _download_sync
from telegram_share_bot.media.work import MediaWorkLease

logger = logging.getLogger(__name__)

_ACTIVE_DOWNLOAD_DIRS: set[Path] = set()
_ACTIVE_DOWNLOAD_DIRS_LOCK = threading.Lock()


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
        finally:
            _forget_active_download_dir(work_dir_holder[0])


def _remember_active_download_dir(path: Path, active: bool) -> None:
    resolved = path.resolve(strict=False)
    with _ACTIVE_DOWNLOAD_DIRS_LOCK:
        if active:
            _ACTIVE_DOWNLOAD_DIRS.add(resolved)
        else:
            _ACTIVE_DOWNLOAD_DIRS.discard(resolved)


def _forget_active_download_dir(path: Path) -> None:
    _remember_active_download_dir(path, False)


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
    work_lease: MediaWorkLease | None = None,
    on_worker_registered: Callable[[asyncio.Task[Any]], None] | None = None,
) -> DownloadedMedia:
    owns_lease = work_lease is None
    active_lease = work_lease or MediaWorkLease(
        None, time.monotonic() + max(0, timeout_seconds)
    )
    abort_event = threading.Event()
    work_dir_holder: list[Path] = []
    worker_holder: list[asyncio.Task[Any]] = []

    def register_worker(worker: asyncio.Task[Any]) -> None:
        worker_holder.append(worker)
        if on_worker_registered is not None:
            on_worker_registered(worker)

    def register_work_dir(path: Path, active: bool) -> None:
        _remember_active_download_dir(path, active)

    try:
        remaining = active_lease.remaining_seconds()
        return cast(
            DownloadedMedia,
            await active_lease.run_sync(
                _download_sync,
                url,
                download_dir,
                max_file_bytes,
                remaining,
                abort_event,
                work_dir_holder,
                wait_timeout_seconds=remaining,
                on_worker_registered=register_worker,
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
                active_directory_callback=register_work_dir,
            ),
        )
    except asyncio.CancelledError:
        abort_event.set()
        if worker_holder:
            worker_holder[-1].add_done_callback(
                lambda task: _cleanup_finished_download(task, work_dir_holder)
            )
        raise
    except TimeoutError as exc:
        abort_event.set()
        if worker_holder:
            worker_holder[-1].add_done_callback(
                lambda task: _cleanup_finished_download(task, work_dir_holder)
            )
        raise DownloadError(
            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
        ) from exc
    except BaseException:
        if work_dir_holder:
            _forget_active_download_dir(work_dir_holder[0])
        raise
    finally:
        if owns_lease:
            await active_lease.release()


def cleanup_media(media: DownloadedMedia) -> None:
    path = media.path
    parent = path.parent
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    if parent.name and parent != path:
        try:
            _cleanup_dir(parent)
        except OSError:
            logger.warning("Could not clean completed media directory")
        finally:
            _forget_active_download_dir(parent)


def cleanup_stale_downloads(
    download_dir: Path,
    max_age_seconds: int = 3600,
) -> int:
    """Remove abandoned download subdirectories older than max_age_seconds.

    Returns the number of directories removed. Top-level files (e.g. the cache DB)
    are left untouched.
    """
    if (
        download_dir.is_symlink()
        or not download_dir.exists()
        or not download_dir.is_dir()
    ):
        return 0

    cutoff = time.time() - max_age_seconds
    try:
        root = download_dir.resolve(strict=True)
    except OSError:
        return 0
    with _ACTIVE_DOWNLOAD_DIRS_LOCK:
        active_dirs = set(_ACTIVE_DOWNLOAD_DIRS)
    removed = 0
    for child in download_dir.iterdir():
        if (
            child.is_symlink()
            or not child.is_dir()
            or re.fullmatch(r"[0-9a-f]{32}", child.name) is None
        ):
            continue
        try:
            resolved = child.resolve(strict=True)
            if resolved.parent != root or resolved in active_dirs:
                continue
            mtime = child.lstat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            logger.info("Removing stale download directory: %s", child.name)
            _cleanup_dir(child)
        except OSError:
            logger.debug("Could not remove stale download directory", exc_info=True)
            continue
        if not child.exists():
            removed += 1
    return removed
