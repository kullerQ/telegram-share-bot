"""Shared inline handler state, limits, keyboards, and query helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
)
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.config import Settings
from telegram_share_bot.media.models import (
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.requests import format_time_range
from telegram_share_bot.storage.media_cache import MediaCache
from telegram_share_bot.storage.user_settings import UserSharingSettings

logger = logging.getLogger("telegram_share_bot.handlers")

_STALE_INLINE_QUERY_MARKERS = (
    "query is too old",
    "query id is invalid",
)

_CALLBACK_PREFIX = "cancel:"
_RETRY_PREFIX = "retry:"
_FULL_FALLBACK_PREFIX = "fallback:"
_CLIP_CALLBACK_PREFIX = "clip:"
_CLIP_AUDIO_CALLBACK_PREFIX = "clipaudio:"
_FULL_CALLBACK_PREFIX = "full:"
_FULL_AUDIO_CALLBACK_PREFIX = "fullaudio:"
_VIDEO_CALLBACK_PREFIX = "video:"
_VIDEO_BEST_CALLBACK_PREFIX = "video-best:"
_VIDEO_BALANCED_CALLBACK_PREFIX = "video-balanced:"
_AUDIO_CALLBACK_PREFIX = "audio:"
_DIRECT_CHOICE_CANCEL_PREFIX = "direct-cancel:"
_CLIP_BEST_CALLBACK_PREFIX = "clip-best:"
_CLIP_BALANCED_CALLBACK_PREFIX = "clip-balanced:"
_FULL_BEST_CALLBACK_PREFIX = "full-best:"
_FULL_BALANCED_CALLBACK_PREFIX = "full-balanced:"
_PENDING_KEY = "pending_inline"
_PENDING_CLIP_KEY = "pending_clip_choice"
_TASKS_KEY = "inline_prepare_tasks"
_CANCELLED_KEY = "cancelled_inline"
_USER_DOWNLOADS_KEY = "user_download_counts"
_USER_DOWNLOADS_LOCK_KEY = "user_download_lock"
_USER_COOLDOWN_KEY = "user_download_cooldowns"
_USER_REQUEST_TIMES_KEY = "user_download_request_times"
_USER_REQUEST_CLEANUP_KEY = "user_download_request_cleanup"
_EMPTY_KEYBOARD = InlineKeyboardMarkup([])

_MAX_PENDING_INLINE = 1000
_MAX_PENDING_CLIP = 500
_MAX_CANCELLED_INLINE = 500
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BASE_DELAY_SECONDS = 1.5
_DOWNLOAD_REQUEST_WINDOW_SECONDS = 60.0

def _preference_quality_label(policy: VideoQualityPolicy) -> str:
    return {
        VideoQualityPolicy.AUTO: "Auto",
        VideoQualityPolicy.BEST: "Best",
        VideoQualityPolicy.BALANCED: "Balanced",
    }[policy]


@dataclass(frozen=True, slots=True)
class PendingInline:
    url: str
    custom_caption: str | None = None
    time_range: TimeRange | None = None
    media_format: MediaFormat = MediaFormat.VIDEO
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO
    preferences: UserSharingSettings = field(default_factory=UserSharingSettings)
    owner_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class PendingClipChoice:
    url: str
    custom_caption: str | None
    time_range: TimeRange | None
    chat_id: int
    preferences: UserSharingSettings = field(default_factory=UserSharingSettings)
    owner_user_id: int | None = None


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    settings = context.application.bot_data.get("settings")
    if not isinstance(settings, Settings):
        raise TypeError("Settings were not attached to the application.")
    return settings


def _is_user_allowed(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    settings = context.application.bot_data.get("settings")
    if not isinstance(settings, Settings):
        return False
    allowed = settings.allowed_user_ids
    if allowed:
        return user_id is not None and user_id in allowed
    return settings.allow_public


def _cache(context: ContextTypes.DEFAULT_TYPE) -> MediaCache:
    cache = context.application.bot_data.get("media_cache")
    if not isinstance(cache, MediaCache):
        raise TypeError("MediaCache was not attached to the application.")
    return cache


def _cache_quality_policy(media_format: MediaFormat, quality_policy: VideoQualityPolicy) -> str:
    return quality_policy.value if media_format is MediaFormat.VIDEO else "best-fit"


@contextlib.asynccontextmanager
async def _unlimited_download_slot() -> AsyncIterator[None]:
    yield


def _download_slot(
    context: ContextTypes.DEFAULT_TYPE,
) -> asyncio.Semaphore | contextlib.AbstractAsyncContextManager[None]:
    """Global download limiter; unlimited when semaphore is unset/disabled."""
    sem = context.application.bot_data.get("download_semaphore")
    if isinstance(sem, asyncio.Semaphore):
        return sem
    return _unlimited_download_slot()


def _user_download_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    lock = context.application.bot_data.get(_USER_DOWNLOADS_LOCK_KEY)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        context.application.bot_data[_USER_DOWNLOADS_LOCK_KEY] = lock
    return lock


def _user_download_counts(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    raw = context.application.bot_data.setdefault(_USER_DOWNLOADS_KEY, {})
    return cast(dict[int, int], raw)


def _user_cooldowns(context: ContextTypes.DEFAULT_TYPE) -> dict[int, float]:
    raw = context.application.bot_data.setdefault(_USER_COOLDOWN_KEY, {})
    return cast(dict[int, float], raw)


def _user_request_times(context: ContextTypes.DEFAULT_TYPE) -> dict[int, deque[float]]:
    raw = context.application.bot_data.setdefault(_USER_REQUEST_TIMES_KEY, {})
    return cast(dict[int, deque[float]], raw)


async def _try_acquire_user_download_slot(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> str | None:
    """Apply per-user request and in-flight limits.

    Returns None on success, or a user-facing error string on denial.
    ``max_downloads_per_user == 0`` disables the per-user in-flight cap.
    Requests denied by the cooldown or in-flight cap still count toward the
    rolling request limit.
    """
    if user_id is None:
        return strings.ACCESS_DENIED
    settings = _settings(context)
    max_per_user = settings.max_downloads_per_user
    max_per_minute = settings.max_downloads_per_minute
    cooldown = settings.download_cooldown_seconds
    now = time.monotonic()
    async with _user_download_lock(context):
        if max_per_minute > 0:
            request_times = _user_request_times(context)
            last_cleanup = context.application.bot_data.get(_USER_REQUEST_CLEANUP_KEY, now)
            if now - last_cleanup >= _DOWNLOAD_REQUEST_WINDOW_SECONDS:
                cutoff = now - _DOWNLOAD_REQUEST_WINDOW_SECONDS
                for tracked_user_id, tracked_timestamps in tuple(request_times.items()):
                    while tracked_timestamps and tracked_timestamps[0] <= cutoff:
                        tracked_timestamps.popleft()
                    if not tracked_timestamps:
                        request_times.pop(tracked_user_id, None)
                context.application.bot_data[_USER_REQUEST_CLEANUP_KEY] = now

            timestamps = request_times.get(user_id)
            if timestamps is None:
                timestamps = deque()
                request_times[user_id] = timestamps
            cutoff = now - _DOWNLOAD_REQUEST_WINDOW_SECONDS
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= max_per_minute:
                return strings.DOWNLOADS_PER_MINUTE_LIMITED.format(limit=max_per_minute)
            timestamps.append(now)

        counts = _user_download_counts(context)
        cooldowns = _user_cooldowns(context)
        last_started = cooldowns.get(user_id)
        if cooldown > 0 and last_started is not None and now - last_started < cooldown:
            return strings.COOLDOWN_LIMITED
        current = counts.get(user_id, 0)
        if max_per_user > 0 and current >= max_per_user:
            return strings.RATE_LIMITED
        counts[user_id] = current + 1
        cooldowns[user_id] = now
        return None


async def _release_user_download_slot(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> None:
    if user_id is None:
        return
    async with _user_download_lock(context):
        counts = _user_download_counts(context)
        current = counts.get(user_id, 0)
        if current <= 1:
            counts.pop(user_id, None)
        else:
            counts[user_id] = current - 1


def _pending_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, PendingInline]:
    raw = context.application.bot_data.setdefault(_PENDING_KEY, {})
    return cast(dict[str, PendingInline], raw)


def _store_pending_url(
    context: ContextTypes.DEFAULT_TYPE,
    result_id: str,
    url: str,
    custom_caption: str | None = None,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    preferences: UserSharingSettings | None = None,
    owner_user_id: int | None = None,
) -> None:
    pending = _pending_map(context)
    pending[result_id] = PendingInline(
        url=url,
        custom_caption=custom_caption,
        time_range=time_range,
        media_format=media_format,
        quality_policy=quality_policy,
        preferences=preferences or UserSharingSettings(),
        owner_user_id=owner_user_id,
    )
    while len(pending) > _MAX_PENDING_INLINE:
        try:
            pending.pop(next(iter(pending)))
        except (KeyError, StopIteration):
            break


def _pending_clip_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, PendingClipChoice]:
    raw = context.application.bot_data.setdefault(_PENDING_CLIP_KEY, {})
    return cast(dict[str, PendingClipChoice], raw)


def _store_pending_clip_choice(
    context: ContextTypes.DEFAULT_TYPE,
    choice_id: str,
    pending: PendingClipChoice,
) -> None:
    choices = _pending_clip_map(context)
    choices[choice_id] = pending
    while len(choices) > _MAX_PENDING_CLIP:
        try:
            choices.pop(next(iter(choices)))
        except (KeyError, StopIteration):
            break


def _format_choice_keyboard(
    choice_id: str,
    preferred_quality: VideoQualityPolicy = VideoQualityPolicy.AUTO,
) -> InlineKeyboardMarkup:
    preferred_prefix = {
        VideoQualityPolicy.BEST: _VIDEO_BEST_CALLBACK_PREFIX,
        VideoQualityPolicy.BALANCED: _VIDEO_BALANCED_CALLBACK_PREFIX,
        VideoQualityPolicy.AUTO: _VIDEO_CALLBACK_PREFIX,
    }[preferred_quality]
    rows = [
        [
            InlineKeyboardButton(
                strings.DIRECT_VIDEO_BUTTON.format(
                    quality=_preference_quality_label(preferred_quality)
                ),
                callback_data=f"{preferred_prefix}{choice_id}",
                style=KeyboardButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                strings.DIRECT_AUDIO_BUTTON,
                callback_data=f"{_AUDIO_CALLBACK_PREFIX}{choice_id}",
            ),
        ]
    ]
    rows.append(
        [
            InlineKeyboardButton(
                strings.DIRECT_CANCEL_BUTTON,
                callback_data=f"{_DIRECT_CHOICE_CANCEL_PREFIX}{choice_id}",
                style=KeyboardButtonStyle.DANGER,
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _clip_choice_keyboard(
    choice_id: str,
    time_range: TimeRange,
    preferred_quality: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    preferred_format: MediaFormat | None = None,
) -> InlineKeyboardMarkup:
    range_label = format_time_range(time_range)
    rows: list[list[InlineKeyboardButton]] = []
    for row_index, (quality_options, audio_prefix, video_label, audio_label) in enumerate(
        (
            (
                (
                    (VideoQualityPolicy.BEST, _CLIP_BEST_CALLBACK_PREFIX),
                    (VideoQualityPolicy.BALANCED, _CLIP_BALANCED_CALLBACK_PREFIX),
                    (VideoQualityPolicy.AUTO, _CLIP_CALLBACK_PREFIX),
                ),
                _CLIP_AUDIO_CALLBACK_PREFIX,
                strings.DIRECT_CLIP_VIDEO_BUTTON.format(
                    quality=_preference_quality_label(preferred_quality), range_label=range_label
                ),
                strings.DIRECT_CLIP_AUDIO_BUTTON.format(range_label=range_label),
            ),
            (
                (
                    (VideoQualityPolicy.BEST, _FULL_BEST_CALLBACK_PREFIX),
                    (VideoQualityPolicy.BALANCED, _FULL_BALANCED_CALLBACK_PREFIX),
                    (VideoQualityPolicy.AUTO, _FULL_CALLBACK_PREFIX),
                ),
                _FULL_AUDIO_CALLBACK_PREFIX,
                strings.DIRECT_FULL_VIDEO_BUTTON.format(
                    quality=_preference_quality_label(preferred_quality)
                ),
                strings.DIRECT_FULL_AUDIO_BUTTON,
            ),
        )
    ):
        video_prefix = next(
            prefix for policy, prefix in quality_options if policy is preferred_quality
        )
        rows.append(
            [
                InlineKeyboardButton(
                    video_label,
                    callback_data=f"{video_prefix}{choice_id}",
                    style=(
                        KeyboardButtonStyle.PRIMARY
                        if row_index == 0 and preferred_format is not MediaFormat.AUDIO
                        else None
                    ),
                ),
                InlineKeyboardButton(
                    audio_label,
                    callback_data=f"{audio_prefix}{choice_id}",
                    style=(
                        KeyboardButtonStyle.PRIMARY
                        if row_index == 0 and preferred_format is MediaFormat.AUDIO
                        else None
                    ),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                strings.DIRECT_CANCEL_BUTTON,
                callback_data=f"{_DIRECT_CHOICE_CANCEL_PREFIX}{choice_id}",
                style=KeyboardButtonStyle.DANGER,
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _task_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, asyncio.Task[Any]]:
    raw = context.application.bot_data.setdefault(_TASKS_KEY, {})
    return cast(dict[str, asyncio.Task[Any]], raw)


def _cancelled_set(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    raw = context.application.bot_data.setdefault(_CANCELLED_KEY, set())
    return cast(set[str], raw)


def _record_cancelled_inline(context: ContextTypes.DEFAULT_TYPE, inline_message_id: str) -> None:
    cancelled = _cancelled_set(context)
    cancelled.add(inline_message_id)
    while len(cancelled) > _MAX_CANCELLED_INLINE:
        try:
            cancelled.pop()
        except KeyError:
            break


def _is_stale_inline_query_error(exc: BadRequest) -> bool:
    message = (exc.message or str(exc)).lower()
    return any(marker in message for marker in _STALE_INLINE_QUERY_MARKERS)


async def _answer_inline_query(
    query: InlineQuery,
    *,
    results: list[InlineQueryResultArticle],
    cache_time: int,
    is_personal: bool,
) -> None:
    """Answer an inline query, ignoring expired/invalid query ids."""
    try:
        await query.answer(
            results=results,
            cache_time=cache_time,
            is_personal=is_personal,
        )
    except BadRequest as exc:
        if _is_stale_inline_query_error(exc):
            logger.warning("Ignoring stale inline query: %s", exc.message)
            return
        raise
