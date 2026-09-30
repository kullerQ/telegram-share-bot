"""Shared inline handler state, limits, keyboards, and query helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

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
from telegram_share_bot.media.models import MediaFormat, TimeRange, VideoQualityPolicy
from telegram_share_bot.media.requests import format_time_range
from telegram_share_bot.media.work import MediaWorkLease, MediaWorkSupervisor
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
_ACTIVE_KEY = "active_inline_requests"
_INLINE_RESERVATION_LOCK_KEY = "inline_reservation_lock"
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
_FINALIZER_TASKS: set[asyncio.Task[None]] = set()


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        with contextlib.suppress(BaseException):
            task.exception()


@dataclass(slots=True)
class UserDownloadLease:
    """Idempotent ownership of one per-user in-flight slot."""

    user_id: int
    _release_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _worker_count: int = field(default=0, repr=False)
    _release_requested: bool = field(default=False, repr=False)

    def track_worker(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        worker: asyncio.Task[Any],
    ) -> None:
        if self._release_requested or self._release_task is not None:
            raise RuntimeError("Cannot attach media work after releasing a user lease")
        self._worker_count += 1

        def on_done(_worker: asyncio.Task[Any]) -> None:
            self._worker_count = max(0, self._worker_count - 1)
            if self._release_requested and self._worker_count == 0:
                self._schedule_release(context)

        worker.add_done_callback(on_done)

    def _schedule_release(self, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Task[None]:
        if self._release_task is None:
            task = asyncio.create_task(
                _release_user_download_slot(context, self.user_id),
                name=f"release-user-download-{self.user_id}",
            )
            task.add_done_callback(_consume_task_exception)
            self._release_task = task
        return self._release_task

    async def release(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._release_requested = True
        if self._worker_count > 0:
            return
        await asyncio.shield(self._schedule_release(context))


@dataclass(slots=True)
class ActiveInlineRequest:
    """One generation currently owning an inline message and user lease."""

    owner_user_id: int
    generation: str
    result_id: str
    lease: UserDownloadLease
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False

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


async def _acquire_download_slot(
    context: ContextTypes.DEFAULT_TYPE,
    timeout_seconds: float,
) -> MediaWorkLease:
    """Acquire bounded global media capacity before any expensive preparation."""
    supervisor = context.application.bot_data.get("media_work_supervisor")
    if not isinstance(supervisor, MediaWorkSupervisor):
        semaphore = context.application.bot_data.get("download_semaphore")
        supervisor = MediaWorkSupervisor(
            0,
            max_waiters=32,
            semaphore=semaphore if isinstance(semaphore, asyncio.Semaphore) else None,
        )
    return await supervisor.acquire(timeout_seconds)


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
        request_times = _user_request_times(context)
        last_cleanup = context.application.bot_data.get(_USER_REQUEST_CLEANUP_KEY)
        if not isinstance(last_cleanup, (int, float)) or (
            now - last_cleanup >= _DOWNLOAD_REQUEST_WINDOW_SECONDS
        ):
            cutoff = now - _DOWNLOAD_REQUEST_WINDOW_SECONDS
            for tracked_user_id, tracked_timestamps in tuple(request_times.items()):
                while tracked_timestamps and tracked_timestamps[0] <= cutoff:
                    tracked_timestamps.popleft()
                if not tracked_timestamps:
                    request_times.pop(tracked_user_id, None)
            cooldowns = _user_cooldowns(context)
            cooldown_expiry = now - max(_DOWNLOAD_REQUEST_WINDOW_SECONDS, cooldown)
            for tracked_user_id, last_started_at in tuple(cooldowns.items()):
                if last_started_at <= cooldown_expiry:
                    cooldowns.pop(tracked_user_id, None)
            context.application.bot_data[_USER_REQUEST_CLEANUP_KEY] = now

        if max_per_minute > 0:
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


def _active_inline_map(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, ActiveInlineRequest]:
    raw = context.application.bot_data.setdefault(_ACTIVE_KEY, {})
    return cast(dict[str, ActiveInlineRequest], raw)


def _inline_reservation_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    lock = context.application.bot_data.get(_INLINE_RESERVATION_LOCK_KEY)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        context.application.bot_data[_INLINE_RESERVATION_LOCK_KEY] = lock
    return lock


async def _reserve_inline_request(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    inline_message_id: str,
    result_id: str,
    user_id: int | None,
) -> tuple[ActiveInlineRequest | None, str | None]:
    """Atomically reserve message ownership and a user's in-flight lease."""
    async with _inline_reservation_lock(context):
        active = _active_inline_map(context).get(inline_message_id)
        if active is not None:
            if active.task is None or not active.task.done():
                denial = await _try_acquire_user_download_slot(context, user_id)
                if denial is not None:
                    return None, denial
                await _release_user_download_slot(context, user_id)
                return None, strings.INLINE_ALREADY_PREPARING
            _active_inline_map(context).pop(inline_message_id, None)
            if _task_map(context).get(inline_message_id) is active.task:
                _task_map(context).pop(inline_message_id, None)
            _cancelled_set(context).discard(inline_message_id)
            await active.lease.release(context)

        denial = await _try_acquire_user_download_slot(context, user_id)
        if denial is not None:
            return None, denial
        assert user_id is not None
        active = ActiveInlineRequest(
            owner_user_id=user_id,
            generation=uuid4().hex,
            result_id=result_id,
            lease=UserDownloadLease(user_id),
        )
        _active_inline_map(context)[inline_message_id] = active
        return active, None


def _attach_inline_task(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str,
    active: ActiveInlineRequest,
    task: asyncio.Task[Any],
) -> bool:
    """Attach a task to its reservation and finalize even before coroutine entry."""
    attached = _active_inline_map(context).get(inline_message_id) is active and not active.cancelled
    if not attached:
        task.cancel()
    else:
        active.task = task
        _task_map(context)[inline_message_id] = task

    def on_done(done_task: asyncio.Task[Any]) -> None:
        if not done_task.cancelled():
            with contextlib.suppress(BaseException):
                done_task.exception()
        finalizer = asyncio.create_task(
            _finalize_inline_request(context, inline_message_id, active),
            name=f"finalize-inline-{active.generation}",
        )
        _FINALIZER_TASKS.add(finalizer)

        def forget_finalizer(completed: asyncio.Task[None]) -> None:
            _FINALIZER_TASKS.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(BaseException):
                error = completed.exception()
                if error is not None:
                    logger.error(
                        "Inline request finalizer failed category=%s",
                        type(error).__name__,
                    )

        finalizer.add_done_callback(forget_finalizer)

    task.add_done_callback(on_done)
    return attached


async def _finalize_inline_request(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str,
    active: ActiveInlineRequest,
) -> None:
    """Release one generation once and never remove a newer replacement."""
    async with _inline_reservation_lock(context):
        active_map = _active_inline_map(context)
        if active_map.get(inline_message_id) is active:
            active_map.pop(inline_message_id, None)
            tasks = _task_map(context)
            if tasks.get(inline_message_id) is active.task:
                tasks.pop(inline_message_id, None)
        _cancelled_set(context).discard(inline_message_id)
    await active.lease.release(context)


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
