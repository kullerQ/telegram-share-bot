"""Bounded source transfer and format fallback orchestration."""

from __future__ import annotations

import copy
import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
)
from telegram_share_bot.media.clips import (
    _download_ranges_param,
    _download_youtube_hls_clip,
    _ensure_clip_within_max,
    _resolve_clip_range,
)
from telegram_share_bot.media.duration import ensure_full_media_duration
from telegram_share_bot.media.files import (
    _classify,
    _cleanup_dir,
    _resolve_downloaded_path,
)
from telegram_share_bot.media.formats import (
    _SOURCE_SIZE_MULTIPLIER,
    _default_video_selector,
    _format_bytes,
    _is_audio_unavailable_error,
    _set_attempt_format_selector,
    _set_attempt_output_template,
    _video_quality_for_selector,
    select_download_candidates,
)
from telegram_share_bot.media.metadata import (
    _evict_extract_info,
    _extract_info_cached,
    _looks_like_stale_cdn_url,
    _pick_info,
    ensure_video_available,
    source_title_and_duration,
)
from telegram_share_bot.media.models import (
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
    VideoUnavailableError,
    _is_transient_download_error,
)
from telegram_share_bot.media.network import create_youtube_dl
from telegram_share_bot.media.progress import TransferMonitor
from telegram_share_bot.media.requests import (
    format_time_range,
)
from telegram_share_bot.media.security import (
    _safe_dns_resolution,
    is_allowed_media_host,
    is_https_url,
    is_safe_media_url,
)
from telegram_share_bot.media.tiktok_delivery import try_tiktok_slideshow
from telegram_share_bot.media.transcode import _convert_audio_for_telegram, _optimize_video_file
from telegram_share_bot.platforms.urls import is_youtube_url, safe_url_for_log

logger = logging.getLogger(__name__)


def _download_sync(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
    abort_event: threading.Event | None = None,
    work_dir_holder: list[Path] | None = None,
    *,
    https_only: bool = False,
    allowed_hosts: frozenset[str] | None = None,
    slideshow_slide_ms: int = 2500,
    slideshow_max_images: int = 35,
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    max_estimated_download_seconds: int = DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    on_optimizing: Callable[[], None] | None = None,
    active_directory_callback: Callable[[Path, bool], None] | None = None,
) -> DownloadedMedia:
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    source_limit = max_file_bytes * _SOURCE_SIZE_MULTIPLIER
    if not is_allowed_media_host(url, allowed_hosts):
        raise DownloadError(strings.DOWNLOAD_HOST_NOT_ALLOWED)
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    if time_range is not None:
        _ensure_clip_within_max(time_range)
        if shutil.which("ffmpeg") is None:
            raise DownloadError(strings.DOWNLOAD_CLIP_FFMPEG_MISSING)

    work_dir = download_dir / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    if active_directory_callback is not None:
        active_directory_callback(work_dir, True)

    def cleanup_work_dir() -> None:
        try:
            _cleanup_dir(work_dir)
        except OSError:
            logger.debug("Could not clean failed download directory", exc_info=True)
        finally:
            if active_directory_callback is not None:
                active_directory_callback(work_dir, False)
    if work_dir_holder is not None:
        work_dir_holder.append(work_dir)

    # Clip ranges only apply to YouTube; try a TikTok slideshow for full sends.
    if time_range is None:
        try:
            slideshow = try_tiktok_slideshow(
                url,
                work_dir,
                source_limit=source_limit,
                max_file_bytes=max_file_bytes,
                deadline=deadline,
                slide_ms=slideshow_slide_ms,
                max_images=slideshow_max_images,
                images_loop=slideshow_images_loop,
                abort_event=abort_event,
                https_only=https_only,
                allowed_hosts=allowed_hosts,
                media_format=media_format,
                max_media_duration_seconds=max_media_duration_seconds,
                on_optimizing=on_optimizing,
            )
        except BaseException:
            cleanup_work_dir()
            raise
        if slideshow is not None:
            return slideshow

    outtmpl = str(work_dir / "%(title).80B [%(id)s].%(ext)s")
    is_clip = time_range is not None

    monitor = TransferMonitor(
        source_limit=source_limit,
        timeout_seconds=timeout_seconds,
        is_clip=is_clip,
        abort_event=abort_event,
        max_estimated_download_seconds=max_estimated_download_seconds,
        monotonic=lambda: time.monotonic(),
        logger=logger,
    )

    # yt-dlp selectors are chosen from ranked metadata and retried at lower
    # quality when the measured output exceeds Telegram's limit.
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
        "retries": 2,
        "merge_output_format": "mp4",
        "progress_hooks": [monitor.hook],
        "format": _default_video_selector(time_range, source_limit),
    }

    if not is_clip:
        ydl_opts["max_filesize"] = source_limit

    effective_range = time_range

    try:
        with _safe_dns_resolution():
            with create_youtube_dl(ydl_opts) as ydl:
                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                    )

                # Probe metadata first so live streams abort before any media bytes land.
                # Reuse extract_info from get_direct_stream when still warm.
                metadata_started_at = time.monotonic()
                extracted, from_cache = _extract_info_cached(ydl, url)
                info = _pick_info(extracted)
                ensure_video_available(info, media_format)
                if effective_range is None:
                    ensure_full_media_duration(info.get("duration"), max_media_duration_seconds)
                if is_clip:
                    logger.info(
                        "Clip metadata resolved in %.1fs (%s) for %s",
                        time.monotonic() - metadata_started_at,
                        "cache" if from_cache else "source",
                        safe_url_for_log(url),
                    )

                if effective_range is not None:
                    effective_range = _resolve_clip_range(effective_range, info)
                    ydl.params["download_ranges"] = _download_ranges_param(effective_range)

                if (
                    effective_range is not None
                    and is_youtube_url(url)
                    and quality_policy is not VideoQualityPolicy.BEST
                ):
                    hls_clip = _download_youtube_hls_clip(
                        info,
                        work_dir=work_dir,
                        time_range=effective_range,
                        media_format=media_format,
                        max_file_bytes=max_file_bytes,
                        deadline=deadline,
                        timeout_seconds=timeout_seconds,
                        abort_event=abort_event,
                        on_optimizing=on_optimizing,
                        quality_policy=quality_policy,
                        max_estimated_download_seconds=max_estimated_download_seconds,
                    )
                    if hls_clip is not None:
                        return hls_clip

                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                    )

                duration_scale, candidates, selectors = select_download_candidates(
                    info,
                    time_range=effective_range,
                    media_format=media_format,
                    quality_policy=quality_policy,
                    max_file_bytes=max_file_bytes,
                    source_limit=source_limit,
                )

                title, duration = source_title_and_duration(info, effective_range)
                best_oversized: tuple[Path, int] | None = None
                last_error: BaseException | None = None
                refreshed = False

                for attempt, selector in enumerate(selectors, start=1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                        )
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                        )
                    attempt_dir = work_dir / f"attempt_{attempt}"
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    _set_attempt_output_template(
                        ydl, str(attempt_dir / "%(title).80B [%(id)s].%(ext)s")
                    )
                    _set_attempt_format_selector(ydl, selector)
                    monitor.start_attempt(
                        can_step_down=(
                            media_format is MediaFormat.VIDEO
                            and quality_policy is VideoQualityPolicy.AUTO
                            and max_estimated_download_seconds > 0
                            and attempt < len(selectors)
                        )
                    )
                    estimate = next(
                        (
                            candidate.estimated_size
                            for candidate in candidates
                            if candidate.selector == selector
                        ),
                        None,
                    )
                    if estimate is not None and estimate > source_limit:
                        logger.info(
                            "Selected source exceeds download bound: "
                            "format=%s estimated_bytes=%d limit_bytes=%d",
                            selector,
                            estimate,
                            source_limit,
                        )
                        last_error = DownloadError(
                            strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                                max_mb=source_limit // (1024 * 1024)
                            )
                        )
                        continue
                    monitor.video_estimated_bytes = None
                    if media_format is MediaFormat.VIDEO:
                        video_format_id = selector.split("+", maxsplit=1)[0]
                        formats = info.get("formats")
                        if isinstance(formats, list):
                            for item in formats:
                                if (
                                    isinstance(item, dict)
                                    and item.get("format_id") == video_format_id
                                ):
                                    monitor.video_estimated_bytes = _format_bytes(
                                        item,
                                        duration_scale,
                                        (
                                            float(effective_range.duration_seconds)
                                            if effective_range is not None
                                            and effective_range.duration_seconds is not None
                                            else None
                                        ),
                                    )
                                    break
                        logger.info(
                            "Video format attempt started: "
                            "format=%s quality=%s estimated_bytes=%s range=%s",
                            selector,
                            _video_quality_for_selector(info, selector),
                            estimate if estimate is not None else "unknown",
                            format_time_range(effective_range)
                            if effective_range is not None
                            else "full",
                        )
                    if effective_range is not None:
                        logger.info(
                            "Clip transfer started: format=%s estimated_bytes=%s range=%s",
                            selector,
                            estimate if estimate is not None else "unknown",
                            format_time_range(effective_range),
                        )
                    try:
                        try:
                            processed = ydl.process_ie_result(
                                copy.deepcopy(extracted), download=True
                            )
                        except yt_dlp.utils.DownloadError as download_exc:
                            if (
                                from_cache
                                and not refreshed
                                and _looks_like_stale_cdn_url(download_exc)
                            ):
                                logger.info(
                                    "Cached extract stale for %s; refreshing before retry",
                                    safe_url_for_log(url),
                                )
                                _evict_extract_info(url)
                                extracted, _ = _extract_info_cached(ydl, url, force_refresh=True)
                                info = _pick_info(extracted)
                                refreshed = True
                                if effective_range is not None:
                                    effective_range = _resolve_clip_range(effective_range, info)
                                    ydl.params["download_ranges"] = _download_ranges_param(
                                        effective_range
                                    )
                                processed = ydl.process_ie_result(
                                    copy.deepcopy(extracted), download=True
                                )
                            else:
                                raise
                        if isinstance(processed, dict):
                            attempt_info = _pick_info(processed)
                        else:
                            attempt_info = info
                        path = _resolve_downloaded_path(attempt_info, attempt_dir, ydl)
                        if not path.exists():
                            raise DownloadError(strings.DOWNLOAD_NO_FILE)
                        if media_format is MediaFormat.AUDIO:
                            path = _convert_audio_for_telegram(
                                path,
                                max_file_bytes=max_file_bytes,
                                deadline=deadline,
                                abort_event=abort_event,
                            )
                        size = path.stat().st_size
                        if size <= 0:
                            raise DownloadError(strings.DOWNLOAD_EMPTY_FILE)
                        if size > source_limit:
                            raise DownloadError(
                                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                                    max_mb=source_limit // (1024 * 1024)
                                )
                            )
                        if size <= max_file_bytes:
                            if media_format is MediaFormat.VIDEO:
                                path = _optimize_video_file(
                                    path,
                                    max_file_bytes=max_file_bytes,
                                    deadline=deadline,
                                    abort_event=abort_event,
                                    on_optimizing=on_optimizing,
                                )
                            if is_clip:
                                logger.info(
                                    "Clip transfer and processing took %.1fs for %s",
                                    time.monotonic() - monitor.transfer_started_at,
                                    safe_url_for_log(url),
                                )
                            result_path = work_dir / f"selected{path.suffix}"
                            path.replace(result_path)
                            return DownloadedMedia(
                                path=result_path,
                                title=str(attempt_info.get("title") or title)[:64],
                                kind=(
                                    MediaKind.AUDIO
                                    if media_format is MediaFormat.AUDIO
                                    else _classify(result_path)
                                ),
                                duration=duration,
                            )
                        preserved = work_dir / f"oversized_{attempt}{path.suffix}"
                        path.replace(preserved)
                        if best_oversized is None or size < best_oversized[1]:
                            if best_oversized is not None:
                                best_oversized[0].unlink(missing_ok=True)
                            best_oversized = (preserved, size)
                        else:
                            preserved.unlink(missing_ok=True)
                    except (yt_dlp.utils.DownloadError, DownloadError) as exc:
                        last_error = exc
                        if monitor.slow_source.is_set():
                            logger.info(
                                "Video format %s exceeded the %ds estimated download target; "
                                "trying a lower quality",
                                selector,
                                max_estimated_download_seconds,
                            )
                        if not monitor.source_too_large.is_set() and not isinstance(
                            exc, DownloadError
                        ):
                            message = str(exc).split("\n")[-1].strip()
                            logger.debug(
                                "Format candidate %s failed for %s: %s",
                                selector,
                                safe_url_for_log(url),
                                message,
                            )
                    finally:
                        _cleanup_dir(attempt_dir)

                if best_oversized is not None and media_format is MediaFormat.VIDEO:
                    try:
                        optimized_path = _optimize_video_file(
                            best_oversized[0],
                            max_file_bytes=max_file_bytes,
                            deadline=deadline,
                            abort_event=abort_event,
                            on_optimizing=on_optimizing,
                        )
                    except DownloadError as exc:
                        if isinstance(exc, VideoUnavailableError):
                            raise
                        if quality_policy is VideoQualityPolicy.BEST:
                            raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from exc
                        raise
                    return DownloadedMedia(
                        path=optimized_path,
                        title=title,
                        kind=_classify(optimized_path),
                        duration=duration,
                    )
                if isinstance(last_error, DownloadError):
                    if isinstance(last_error, VideoUnavailableError):
                        raise last_error
                    if quality_policy is VideoQualityPolicy.BEST:
                        raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from last_error
                    raise last_error
                if last_error is not None:
                    if (
                        media_format is MediaFormat.VIDEO
                        and quality_policy is VideoQualityPolicy.BEST
                    ):
                        raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from last_error
                    message = str(last_error).split("\n")[-1].strip()
                    if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
                        raise DownloadError(strings.AUDIO_UNAVAILABLE) from last_error
                    raise DownloadError(
                        strings.DOWNLOAD_FAILED_GENERIC,
                        retryable=_is_transient_download_error(message),
                    ) from last_error
                raise DownloadError(strings.DOWNLOAD_NO_FILE)
    except DownloadError:
        cleanup_work_dir()
        raise
    except yt_dlp.utils.DownloadError as exc:
        cleanup_work_dir()
        message = str(exc).split("\n")[-1].strip() or strings.DOWNLOAD_FAILED_GENERIC
        if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
            logger.info("No compatible audio stream available for %s", safe_url_for_log(url))
            raise DownloadError(strings.AUDIO_UNAVAILABLE) from exc
        logger.warning("yt-dlp download error for %s: %s", safe_url_for_log(url), message)
        if "File is larger than max-filesize" in message or "filesize" in message.lower():
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=max_file_bytes // (1024 * 1024))
            ) from exc
        raise DownloadError(
            strings.DOWNLOAD_FAILED_GENERIC,
            retryable=_is_transient_download_error(message),
        ) from exc
    except Exception as exc:
        cleanup_work_dir()
        message = str(exc).split("\n")[-1].strip() or strings.DOWNLOAD_FAILED_GENERIC
        if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
            logger.info("No compatible audio stream available for %s", safe_url_for_log(url))
            raise DownloadError(strings.AUDIO_UNAVAILABLE) from exc
        logger.warning("Download failed for %s: %s", safe_url_for_log(url), message)
        raise DownloadError(
            strings.DOWNLOAD_FAILED_GENERIC,
            retryable=_is_transient_download_error(message),
        ) from exc
