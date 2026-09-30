"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any
from uuid import uuid4

from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.media.direct import get_direct_stream
from telegram_share_bot.media.duration import ensure_full_media_duration
from telegram_share_bot.media.jobs import cleanup_media, download_media
from telegram_share_bot.media.models import (
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.work import MediaWorkLease
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.storage.user_settings import (
    UserSharingSettings,
)

from .delivery import (
    _cancel_keyboard,
    _edit_inline_text,
    _input_media,
    _retry_keyboard,
    _upload_direct_url_for_file_id,
    _upload_for_file_id,
)
from .preferences import _resolve_user_caption, _user_preferences
from .state import (
    _EMPTY_KEYBOARD,
    ActiveInlineRequest,
    _acquire_download_slot,
    _cache,
    _cache_quality_policy,
    _cancelled_set,
    _finalize_inline_request,
    _pending_map,
    _release_user_download_slot,
    _settings,
    _store_pending_url,
    _task_map,
)
from .trace import _download_failure_category, _SendTrace

logger = logging.getLogger("telegram_share_bot.handlers")


async def _prepare_inline_media(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    inline_message_id: str,
    url: str,
    result_id: str,
    user_id: int | None = None,
    custom_caption: str | None = None,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    preferences: UserSharingSettings | None = None,
    active_request: ActiveInlineRequest | None = None,
) -> None:
    settings = _settings(context)
    if preferences is None:
        preferences = await _user_preferences(context, user_id)
    cache = _cache(context)
    cache_quality = _cache_quality_policy(media_format, quality_policy)
    display_url = safe_url_for_log(url)
    media: DownloadedMedia | None = None
    work_lease: MediaWorkLease | None = None
    keep_pending_for_retry = False
    trace = _SendTrace(uuid4().hex[:12], "inline", media_format, time_range, user_id)
    trace.event("start")

    def register_worker(worker: asyncio.Task[Any]) -> None:
        if active_request is not None:
            active_request.lease.track_worker(context, worker)

    try:
        # 1. Attempt instant send from cache
        cached = (
            await cache.get_preferred_video(
                url, time_range=time_range, quality_policy=cache_quality
            )
            if media_format is MediaFormat.VIDEO
            else await cache.get(
                url, time_range=time_range, media_format=media_format, quality_policy=cache_quality
            )
        )
        if cached is not None:
            trace.cache_outcome = "hit"
            try:
                if inline_message_id in _cancelled_set(context):
                    return
                caption = _resolve_user_caption(
                    preferences,
                    settings,
                    media_title=cached.title,
                    original_url=url,
                    custom_caption=custom_caption,
                )
                await context.bot.edit_message_media(
                    media=_input_media(cached.file_id, cached.title, cached.kind, caption=caption),
                    inline_message_id=inline_message_id,
                    reply_markup=_EMPTY_KEYBOARD,
                )
                trace.event("complete")
                return
            except BadRequest:
                trace.event("cache_fallback", failure="invalid_file_id", level=logging.WARNING)
                trace.cache_outcome = "evicted"
                await cache.evict_entry(cached)
            except Exception:
                trace.event("cache_fallback", failure="cache_send", level=logging.WARNING)
                trace.cache_outcome = "evicted"
                await cache.evict_entry(cached)

        # 2. Cache miss or fallback to download pipeline
        if inline_message_id in _cancelled_set(context):
            return
        # The chosen article already contains the checking status and Cancel button.
        # Try direct URL import via Telegram first (fastest, zero local upload bandwidth).
        # Skip for clips — that path would send the whole video.
        queue_started_at = time.monotonic()
        work_lease = await _acquire_download_slot(context, settings.download_timeout_seconds)
        if time.monotonic() - queue_started_at >= 0.1:
            trace.event("queue", stage_started_at=queue_started_at)
        if time_range is None and media_format is MediaFormat.AUDIO:
            discovery_started_at = time.monotonic()
            direct_stream = await get_direct_stream(
                url=url,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=min(15, settings.download_timeout_seconds),
                allowed_hosts=settings.allowed_media_hosts,
                media_format=media_format,
                https_only=settings.https_only,
                work_lease=work_lease,
                on_worker_registered=register_worker,
            )
            trace.event("discovery", stage_started_at=discovery_started_at)
            if direct_stream is not None:
                ensure_full_media_duration(
                    direct_stream.duration, settings.max_media_duration_seconds
                )
                if inline_message_id in _cancelled_set(context):
                    return
                await _edit_inline_text(
                    context,
                    inline_message_id,
                    strings.INLINE_UPLOADING.format(url=display_url),
                    reply_markup=_cancel_keyboard(result_id),
                )
                file_id_info = await _upload_direct_url_for_file_id(
                    context, settings, direct_stream
                )
                if file_id_info is not None:
                    file_id, title, kind = file_id_info
                    await cache.set(
                        url=url,
                        file_id=file_id,
                        kind=kind,
                        title=title,
                        duration=direct_stream.duration,
                        time_range=None,
                        media_format=media_format,
                        quality_policy=cache_quality,
                    )
                    if inline_message_id in _cancelled_set(context):
                        return
                    caption = _resolve_user_caption(
                        preferences,
                        settings,
                        media_title=title,
                        original_url=url,
                        custom_caption=custom_caption,
                    )
                    await context.bot.edit_message_media(
                        media=_input_media(file_id, title, kind, caption=caption),
                        inline_message_id=inline_message_id,
                        reply_markup=_EMPTY_KEYBOARD,
                    )
                    trace.cache_outcome = "direct_url"
                    trace.event("complete")
                    return
                trace.event("direct_url_fallback", failure="telegram_fetch")

        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_DOWNLOADING.format(url=display_url),
            reply_markup=_cancel_keyboard(result_id),
        )
        loop = asyncio.get_running_loop()

        def show_optimization_status() -> None:
            trace.event("optimization")
            future = asyncio.run_coroutine_threadsafe(
                _edit_inline_text(
                    context,
                    inline_message_id,
                    strings.OPTIMIZING_FOR_TELEGRAM,
                    reply_markup=_cancel_keyboard(result_id),
                ),
                loop,
            )
            with contextlib.suppress(Exception):
                future.result(timeout=5)

        if inline_message_id in _cancelled_set(context):
            return
        download_started_at = time.monotonic()
        media = await download_media(
            url=url,
            download_dir=settings.download_dir,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=settings.download_timeout_seconds,
            allowed_hosts=settings.allowed_media_hosts,
            https_only=settings.https_only,
            slideshow_slide_ms=settings.slideshow_slide_ms,
            slideshow_max_images=settings.slideshow_max_images,
            slideshow_images_loop=settings.slideshow_images_loop,
            time_range=time_range,
            media_format=media_format,
            quality_policy=quality_policy,
            max_estimated_download_seconds=settings.max_estimated_download_seconds,
            max_media_duration_seconds=settings.max_media_duration_seconds,
            on_optimizing=show_optimization_status,
            work_lease=work_lease,
            on_worker_registered=register_worker,
        )
        trace.event("download", stage_started_at=download_started_at)
        if inline_message_id in _cancelled_set(context):
            return
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_UPLOADING.format(url=display_url),
            reply_markup=_cancel_keyboard(result_id),
        )
        upload_started_at = time.monotonic()
        file_id, title, kind, video_height = await _upload_for_file_id(context, settings, media)
        trace.event("upload", stage_started_at=upload_started_at)
        await cache.set(
            url=url,
            file_id=file_id,
            kind=kind,
            title=title,
            duration=media.duration,
            time_range=time_range,
            media_format=media_format,
            quality_policy=cache_quality,
            video_height=video_height,
        )
        if inline_message_id in _cancelled_set(context):
            return
        caption = _resolve_user_caption(
            preferences,
            settings,
            media_title=title,
            original_url=url,
            custom_caption=custom_caption,
        )
        await context.bot.edit_message_media(
            media=_input_media(file_id, title, kind, caption=caption),
            inline_message_id=inline_message_id,
            reply_markup=_EMPTY_KEYBOARD,
        )
        trace.event("complete")
    except asyncio.CancelledError:
        trace.event("cancelled")
        raise
    except DownloadError as exc:
        if inline_message_id in _cancelled_set(context):
            return
        trace.event("failed", failure=_download_failure_category(exc), level=logging.WARNING)
        keep_pending_for_retry = exc.retryable
        if keep_pending_for_retry:
            _store_pending_url(
                context,
                result_id,
                url,
                custom_caption=custom_caption,
                time_range=time_range,
                media_format=media_format,
                quality_policy=quality_policy,
                preferences=preferences,
                owner_user_id=user_id,
            )
        await _edit_inline_text(
            context,
            inline_message_id,
            str(exc) or strings.INLINE_CHOSEN_DOWNLOAD_FAILED,
            reply_markup=(
                _retry_keyboard(result_id, time_range, media_format)
                if keep_pending_for_retry
                else _EMPTY_KEYBOARD
            ),
        )
    except (NetworkError, TimedOut):
        if inline_message_id in _cancelled_set(context):
            return
        trace.event("failed", failure="upload_network", level=logging.WARNING)
        keep_pending_for_retry = True
        _store_pending_url(
            context,
            result_id,
            url,
            custom_caption=custom_caption,
            time_range=time_range,
            media_format=media_format,
            quality_policy=quality_policy,
            preferences=preferences,
            owner_user_id=user_id,
        )
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_UPLOAD_FAILED,
            reply_markup=_retry_keyboard(result_id, time_range, media_format),
        )
    except Exception:
        if inline_message_id in _cancelled_set(context):
            return
        trace.event("failed", failure="unexpected", level=logging.ERROR, exc_info=True)
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_PREPARE_FAILED,
            reply_markup=_EMPTY_KEYBOARD,
        )
    finally:
        if not keep_pending_for_retry:
            _pending_map(context).pop(result_id, None)
        if media is not None:
            try:
                await asyncio.to_thread(cleanup_media, media)
            except Exception:
                logger.warning("Inline media cleanup failed category=filesystem")
        try:
            if work_lease is not None:
                await work_lease.release()
        finally:
            if active_request is not None:
                await _finalize_inline_request(context, inline_message_id, active_request)
            else:
                _task_map(context).pop(inline_message_id, None)
                _cancelled_set(context).discard(inline_message_id)
                await _release_user_download_slot(context, user_id)
