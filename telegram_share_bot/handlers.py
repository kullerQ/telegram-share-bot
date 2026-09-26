"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from telegram_share_bot import platform_icons, platform_previews, strings
from telegram_share_bot.cache import CachedMedia, MediaCache
from telegram_share_bot.config import (
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    CaptionMode,
    Settings,
)
from telegram_share_bot.downloader import (
    MAX_CLIP_SECONDS,
    DirectMediaStream,
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
    cleanup_media,
    download_media,
    ensure_full_media_duration,
    extract_media_request,
    format_time_range,
    get_direct_stream,
    is_allowed_media_host,
    is_https_url,
    resolve_caption,
    sanitize_caption,
)
from telegram_share_bot.normalizer import safe_url_for_log
from telegram_share_bot.user_settings import (
    CaptionPreference,
    UserSettingsStore,
    UserSharingSettings,
)

logger = logging.getLogger(__name__)

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


def _user_settings_store(context: ContextTypes.DEFAULT_TYPE) -> UserSettingsStore:
    store = context.application.bot_data.get("user_settings")
    if isinstance(store, UserSettingsStore):
        return store
    settings = _settings(context)
    path = getattr(settings, "user_settings_db_path", None)
    if not isinstance(path, Path):
        download_dir = getattr(settings, "download_dir", None)
        if not isinstance(download_dir, Path):
            raise TypeError("User settings database path is not configured.")
        path = download_dir / "user_settings.db"
    store = UserSettingsStore(path)
    context.application.bot_data["user_settings"] = store
    return store


async def _user_preferences(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> UserSharingSettings:
    if user_id is None:
        return UserSharingSettings()
    try:
        store = _user_settings_store(context)
    except TypeError:
        return UserSharingSettings()
    return await store.get(user_id)


def _preference_quality_label(policy: VideoQualityPolicy) -> str:
    return {
        VideoQualityPolicy.AUTO: "Auto",
        VideoQualityPolicy.BEST: "Best",
        VideoQualityPolicy.BALANCED: "Balanced",
    }[policy]


def _preference_caption_label(preference: CaptionPreference | None, settings: Settings) -> str:
    if preference is None:
        mode = {
            CaptionMode.MEDIA: "Media title",
            CaptionMode.CUSTOM: "Custom caption",
            CaptionMode.OFF: "None",
        }[settings.caption_mode]
        return f"Bot default · {mode}"
    return {
        CaptionPreference.MEDIA_TITLE: "Media title",
        CaptionPreference.ORIGINAL_LINK: "Original link",
        CaptionPreference.NONE: "None",
    }[preference]


def _preference_format_label(media_format: MediaFormat | None) -> str:
    if media_format is None:
        return "Not specified"
    return "Video" if media_format is MediaFormat.VIDEO else "Audio"


def _resolve_user_caption(
    preferences: UserSharingSettings,
    settings: Settings,
    *,
    media_title: str,
    original_url: str,
    custom_caption: str | None,
) -> str | None:
    if preferences.caption is None:
        return resolve_caption(
            settings.caption_mode,
            media_title=media_title,
            custom_caption=custom_caption,
        )
    if custom_caption is not None:
        return sanitize_caption(custom_caption)
    if preferences.caption is CaptionPreference.MEDIA_TITLE:
        return sanitize_caption(media_title)
    if preferences.caption is CaptionPreference.ORIGINAL_LINK:
        return sanitize_caption(original_url)
    return None


def _settings_text(preferences: UserSharingSettings, settings: Settings) -> str:
    return strings.SETTINGS_MESSAGE.format(
        quality=_preference_quality_label(preferences.video_quality),
        caption=_preference_caption_label(preferences.caption, settings),
        media_format=_preference_format_label(preferences.default_format),
    )


def _settings_keyboard(user_id: int, preferences: UserSharingSettings) -> InlineKeyboardMarkup:
    def choice(field: str, value: str, label: str, selected: bool) -> InlineKeyboardButton:
        marker = "✓ " if selected else ""
        return InlineKeyboardButton(
            f"{marker}{label}", callback_data=f"settings:{user_id}:{field}:{value}"
        )

    return InlineKeyboardMarkup(
        [
            [
                choice(
                    "quality",
                    VideoQualityPolicy.AUTO.value,
                    "Auto",
                    preferences.video_quality is VideoQualityPolicy.AUTO,
                ),
                choice(
                    "quality",
                    VideoQualityPolicy.BEST.value,
                    "Best",
                    preferences.video_quality is VideoQualityPolicy.BEST,
                ),
                choice(
                    "quality",
                    VideoQualityPolicy.BALANCED.value,
                    "Balanced",
                    preferences.video_quality is VideoQualityPolicy.BALANCED,
                ),
            ],
            [
                choice(
                    "caption",
                    CaptionPreference.MEDIA_TITLE.value,
                    "Media title",
                    preferences.caption is CaptionPreference.MEDIA_TITLE,
                ),
                choice(
                    "caption",
                    CaptionPreference.ORIGINAL_LINK.value,
                    "Original link",
                    preferences.caption is CaptionPreference.ORIGINAL_LINK,
                ),
                choice(
                    "caption",
                    CaptionPreference.NONE.value,
                    "None",
                    preferences.caption is CaptionPreference.NONE,
                ),
            ],
            [choice("caption", "default", "Bot default", preferences.caption is None)],
            [
                choice("format", "video", "Video", preferences.default_format is MediaFormat.VIDEO),
                choice("format", "audio", "Audio", preferences.default_format is MediaFormat.AUDIO),
                choice(
                    "format", "unspecified", "Not specified", preferences.default_format is None
                ),
            ],
        ]
    )


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


def _user_request_times(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[int, deque[float]]:
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


def _pending_clip_map(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, PendingClipChoice]:
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
    quality_options = (
        (VideoQualityPolicy.BEST, _VIDEO_BEST_CALLBACK_PREFIX),
        (VideoQualityPolicy.BALANCED, _VIDEO_BALANCED_CALLBACK_PREFIX),
        (VideoQualityPolicy.AUTO, _VIDEO_CALLBACK_PREFIX),
    )
    preferred_prefix = next(
        prefix for policy, prefix in quality_options if policy is preferred_quality
    )
    rows = [
        [
            InlineKeyboardButton(
                strings.DIRECT_VIDEO_BUTTON.format(
                    quality=_preference_quality_label(preferred_quality)
                ),
                callback_data=f"{preferred_prefix}{choice_id}",
            ),
            InlineKeyboardButton(
                strings.DIRECT_AUDIO_BUTTON,
                callback_data=f"{_AUDIO_CALLBACK_PREFIX}{choice_id}",
            ),
        ]
    ]
    alternatives = [item for item in quality_options if item[0] is not preferred_quality]
    for offset in range(0, len(alternatives), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    strings.DIRECT_VIDEO_QUALITY_BUTTON.format(
                        quality=_preference_quality_label(policy)
                    ),
                    callback_data=f"{prefix}{choice_id}",
                )
                for policy, prefix in alternatives[offset : offset + 2]
            ]
        )
    return InlineKeyboardMarkup(rows)


def _clip_choice_keyboard(
    choice_id: str,
    time_range: TimeRange,
    preferred_quality: VideoQualityPolicy = VideoQualityPolicy.AUTO,
) -> InlineKeyboardMarkup:
    range_label = format_time_range(time_range)
    rows: list[list[InlineKeyboardButton]] = []
    for quality_options, audio_prefix, video_label, audio_label, extra_label in (
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
            strings.CLIP_VIDEO_QUALITY_BUTTON,
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
            strings.FULL_VIDEO_QUALITY_BUTTON,
        ),
    ):
        video_prefix = next(
            prefix for policy, prefix in quality_options if policy is preferred_quality
        )
        rows.append(
            [
                InlineKeyboardButton(video_label, callback_data=f"{video_prefix}{choice_id}"),
                InlineKeyboardButton(audio_label, callback_data=f"{audio_prefix}{choice_id}"),
            ]
        )
        alternatives = [item for item in quality_options if item[0] is not preferred_quality]
        for offset in range(0, len(alternatives), 2):
            rows.append(
                [
                    InlineKeyboardButton(
                        extra_label.format(
                            range_label=range_label,
                            quality=_preference_quality_label(policy),
                        ),
                        callback_data=f"{prefix}{choice_id}",
                    )
                    for policy, prefix in alternatives[offset : offset + 2]
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_chat is None:
        return

    if not _is_user_allowed(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(strings.ACCESS_DENIED)
        return

    bot_name = context.bot.first_name or strings.BOT_DISPLAY_NAME
    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    await update.effective_message.reply_text(
        strings.START_MESSAGE.format(bot_name=bot_name, bot_username=bot_username),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    if not _is_user_allowed(context, update.effective_user.id if update.effective_user else None):
        await message.reply_text(strings.ACCESS_DENIED)
        return

    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    caption_mode = _settings(context).caption_mode
    if caption_mode is CaptionMode.CUSTOM:
        caption_help = strings.HELP_CAPTION_CUSTOM
    elif caption_mode is CaptionMode.OFF:
        caption_help = strings.HELP_CAPTION_OFF
    else:
        caption_help = strings.HELP_CAPTION_MEDIA
    await message.reply_text(
        strings.HELP_MESSAGE.format(bot_username=bot_username, caption_help=caption_help),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's private sharing preferences."""
    message = update.effective_message
    chat = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None
    if message is None or chat is None:
        return
    if chat.type != ChatType.PRIVATE:
        await message.reply_text(strings.DIRECT_COMMAND_PRIVATE_ONLY)
        return
    if not _is_user_allowed(context, user_id):
        await message.reply_text(strings.ACCESS_DENIED)
        return
    preferences = await _user_preferences(context, user_id)
    assert user_id is not None
    await message.reply_text(
        _settings_text(preferences, _settings(context)),
        reply_markup=_settings_keyboard(user_id, preferences),
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply an allow-listed settings choice for the originating user."""
    query = update.callback_query
    user_id = query.from_user.id if query and query.from_user else None
    if query is None or query.data is None:
        return
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return
    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "settings" or user_id is None:
        await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
        return
    _, owner_raw, field, value = parts
    if owner_raw != str(user_id):
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return

    store = _user_settings_store(context)
    if field == "quality":
        try:
            selected_quality = VideoQualityPolicy(value)
        except ValueError:
            await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
            return
        preferences = await store.set_quality(user_id, selected_quality)
    elif field == "caption":
        if value == "default":
            preferences = await store.set_caption(user_id, None)
        else:
            try:
                selected_caption = CaptionPreference(value)
            except ValueError:
                await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
                return
            preferences = await store.set_caption(user_id, selected_caption)
    elif field == "format":
        if value == "unspecified":
            preferences = await store.set_format(user_id, None)
        elif value in {MediaFormat.VIDEO.value, MediaFormat.AUDIO.value}:
            preferences = await store.set_format(user_id, MediaFormat(value))
        else:
            await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
            return
    else:
        await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
        return

    await query.answer(text=strings.SETTINGS_SAVED)
    try:
        await query.edit_message_text(
            _settings_text(preferences, _settings(context)),
            reply_markup=_settings_keyboard(user_id, preferences),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download or send cached media when a URL is sent directly in private chat."""
    message = update.effective_message
    if message is None or not message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not _is_user_allowed(context, user_id):
        await message.reply_text(strings.ACCESS_DENIED)
        return

    request = extract_media_request(message.text)
    url = request.url
    custom_caption = request.custom_caption
    time_range = request.time_range
    if url is None:
        await message.reply_text(strings.DIRECT_URL_HINT)
        return

    settings = _settings(context)

    if not is_allowed_media_host(url, settings.allowed_media_hosts):
        await message.reply_text(strings.DOWNLOAD_HOST_NOT_ALLOWED)
        return

    if settings.https_only and not is_https_url(url):
        await message.reply_text(strings.DOWNLOAD_HTTPS_REQUIRED)
        return

    preferences = await _user_preferences(context, user_id)

    if time_range is not None:
        choice_id = uuid4().hex
        _store_pending_clip_choice(
            context,
            choice_id,
            PendingClipChoice(
                url=url,
                custom_caption=custom_caption,
                time_range=time_range,
                chat_id=message.chat_id,
                preferences=preferences,
                owner_user_id=user_id,
            ),
        )
        await message.reply_text(
            strings.DIRECT_CLIP_FORMAT_PROMPT.format(range_label=format_time_range(time_range)),
            reply_markup=_clip_choice_keyboard(choice_id, time_range, preferences.video_quality),
        )
        return

    if preferences.default_format is not None:
        status = await message.reply_text(strings.DIRECT_PREPARING)
        await _run_direct_download(
            context,
            chat_id=message.chat_id,
            user_id=user_id,
            url=url,
            custom_caption=custom_caption,
            time_range=None,
            status_message=status,
            media_format=preferences.default_format,
            quality_policy=(
                preferences.video_quality
                if preferences.default_format is MediaFormat.VIDEO
                else VideoQualityPolicy.AUTO
            ),
            preferences=preferences,
        )
        return

    choice_id = uuid4().hex
    _store_pending_clip_choice(
        context,
        choice_id,
        PendingClipChoice(
            url=url,
            custom_caption=custom_caption,
            time_range=None,
            chat_id=message.chat_id,
            preferences=preferences,
            owner_user_id=user_id,
        ),
    )
    await message.reply_text(
        strings.DIRECT_FORMAT_PROMPT,
        reply_markup=_format_choice_keyboard(choice_id, preferences.video_quality),
    )


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _explicit_format_command(update, context, MediaFormat.AUDIO)


async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _explicit_format_command(update, context, MediaFormat.VIDEO)


async def _explicit_format_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_format: MediaFormat,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or not message.text:
        return
    if chat is None or chat.type != ChatType.PRIVATE:
        await message.reply_text(strings.DIRECT_COMMAND_PRIVATE_ONLY)
        return
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_user_allowed(context, user_id):
        await message.reply_text(strings.ACCESS_DENIED)
        return
    raw_request = message.text.partition(" ")[2].strip()
    preferences = await _user_preferences(context, user_id)
    quality_policy = preferences.video_quality
    if media_format is MediaFormat.VIDEO:
        mode, separator, remainder = raw_request.partition(" ")
        requested_policy = {
            "auto": VideoQualityPolicy.AUTO,
            "best": VideoQualityPolicy.BEST,
            "balanced": VideoQualityPolicy.BALANCED,
        }.get(mode.lower())
        if requested_policy is not None:
            quality_policy = requested_policy
            raw_request = remainder.strip() if separator else ""
    request = extract_media_request(raw_request)
    if request.url is None:
        usage = (
            strings.DIRECT_AUDIO_USAGE
            if media_format is MediaFormat.AUDIO
            else strings.DIRECT_VIDEO_USAGE
        )
        await message.reply_text(usage)
        return
    settings = _settings(context)
    if not is_allowed_media_host(request.url, settings.allowed_media_hosts):
        await message.reply_text(strings.DOWNLOAD_HOST_NOT_ALLOWED)
        return
    if settings.https_only and not is_https_url(request.url):
        await message.reply_text(strings.DOWNLOAD_HTTPS_REQUIRED)
        return
    status = await message.reply_text(strings.DIRECT_PREPARING)
    await _run_direct_download(
        context,
        chat_id=message.chat_id,
        user_id=user_id,
        url=request.url,
        custom_caption=request.custom_caption,
        time_range=request.time_range,
        status_message=status,
        media_format=media_format,
        quality_policy=(
            quality_policy if media_format is MediaFormat.VIDEO else VideoQualityPolicy.AUTO
        ),
        preferences=preferences,
    )


async def _remove_completed_direct_status(status_message: Message) -> None:
    """Remove transient progress text after its media has been delivered."""
    try:
        await status_message.delete()
    except TelegramError:
        logger.debug("Could not remove completed direct status message")


async def _run_direct_download(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int | None,
    url: str,
    custom_caption: str | None,
    time_range: TimeRange | None,
    status_message: Message,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    preferences: UserSharingSettings | None = None,
    skip_cache: bool = False,
) -> None:
    display_url = safe_url_for_log(url)
    cache = _cache(context)
    settings = _settings(context)
    if preferences is None:
        preferences = await _user_preferences(context, user_id)
    cache_quality = _cache_quality_policy(media_format, quality_policy)

    if not skip_cache:
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
            logger.info("Cache hit for direct URL: %s", display_url)
            try:
                await status_message.edit_text(strings.DIRECT_UPLOADING)
                await _send_cached_media_to_chat(
                    context,
                    chat_id,
                    cached,
                    custom_caption=custom_caption,
                    preferences=preferences,
                    original_url=url,
                )
                await _remove_completed_direct_status(status_message)
                return
            except BadRequest as exc:
                logger.warning(
                    "Cached file_id invalid for %s, evicting and falling back: %s",
                    display_url,
                    exc.message,
                )
                await cache.evict_entry(cached)
            except Exception:
                logger.exception("Failed sending cached media for %s, falling back", display_url)
                await cache.evict_entry(cached)

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        await status_message.edit_text(denial)
        return

    media: DownloadedMedia | None = None
    try:
        # Direct URL import skips clips (would fetch the whole video).
        if time_range is None and media_format is MediaFormat.AUDIO:
            direct_stream = await get_direct_stream(
                url=url,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=min(15, settings.download_timeout_seconds),
                allowed_hosts=settings.allowed_media_hosts,
                media_format=media_format,
                https_only=settings.https_only,
            )
            if direct_stream is not None:
                ensure_full_media_duration(
                    direct_stream.duration, settings.max_media_duration_seconds
                )
                await status_message.edit_text(strings.DIRECT_UPLOADING)
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
                    await _send_cached_media_to_chat(
                        context,
                        chat_id,
                        CachedMedia(
                            url=url,
                            file_id=file_id,
                            kind=kind,
                            title=title,
                            duration=direct_stream.duration,
                        ),
                        custom_caption=custom_caption,
                        preferences=preferences,
                        original_url=url,
                    )
                    await _remove_completed_direct_status(status_message)
                    return

        await status_message.edit_text(strings.DIRECT_DOWNLOADING)
        loop = asyncio.get_running_loop()

        def show_optimization_status() -> None:
            future = asyncio.run_coroutine_threadsafe(
                status_message.edit_text(strings.OPTIMIZING_FOR_TELEGRAM), loop
            )
            with contextlib.suppress(Exception):
                future.result(timeout=5)

        async with _download_slot(context):
            media = await download_media(
                url=url,
                download_dir=settings.download_dir,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=settings.download_timeout_seconds,
                allowed_hosts=settings.allowed_media_hosts,
                media_format=media_format,
                quality_policy=quality_policy,
                max_estimated_download_seconds=settings.max_estimated_download_seconds,
                https_only=settings.https_only,
                slideshow_slide_ms=settings.slideshow_slide_ms,
                slideshow_max_images=settings.slideshow_max_images,
                slideshow_images_loop=settings.slideshow_images_loop,
                time_range=time_range,
                max_media_duration_seconds=settings.max_media_duration_seconds,
                on_optimizing=show_optimization_status,
            )
            await status_message.edit_text(strings.DIRECT_UPLOADING)
            sent_msg = await _send_media_to_chat(
                context,
                chat_id,
                media,
                settings=settings,
                custom_caption=custom_caption,
                preferences=preferences,
                original_url=url,
            )
        file_id, result_kind = _file_id_and_kind_from_message(sent_msg)
        await cache.set(
            url=url,
            file_id=file_id,
            kind=result_kind,
            title=media.title,
            duration=media.duration,
            time_range=time_range,
            media_format=media_format,
            quality_policy=cache_quality,
            video_height=_message_video_height(sent_msg),
        )
        await _remove_completed_direct_status(status_message)
    except DownloadError as exc:
        logger.warning("Direct download failed for %s: %s", display_url, exc)
        await status_message.edit_text(str(exc) or strings.DIRECT_DOWNLOAD_FAILED)
    except (NetworkError, TimedOut) as exc:
        logger.warning("Direct upload failed for %s: %s", display_url, exc)
        await status_message.edit_text(strings.DIRECT_UPLOAD_FAILED)
    except Exception:
        logger.exception("Failed to handle direct URL message")
        await status_message.edit_text(strings.DIRECT_SEND_FAILED)
    finally:
        await _release_user_download_slot(context, user_id)
        if media is not None:
            cleanup_media(media)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer quickly; heavy download happens after the result is chosen."""
    query = update.inline_query
    if query is None:
        return

    if not _is_user_allowed(context, query.from_user.id if query.from_user else None):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_ACCESS_DENIED_TITLE,
                    strings.INLINE_ACCESS_DENIED_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    text = (query.query or "").strip()
    if not text:
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_EMPTY_TITLE,
                    strings.INLINE_EMPTY_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    request = extract_media_request(text)
    url = request.url
    custom_caption = request.custom_caption
    time_range = request.time_range
    if url is None:
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.INLINE_NO_URL_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    settings = _settings(context)
    if not is_allowed_media_host(url, settings.allowed_media_hosts):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.DOWNLOAD_HOST_NOT_ALLOWED,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    if settings.https_only and not is_https_url(url):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.DOWNLOAD_HTTPS_REQUIRED,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    user_id = query.from_user.id if query.from_user else None
    preferences = await _user_preferences(context, user_id)

    preview = await platform_previews.resolve_preview(url)

    choices: list[tuple[str, MediaFormat, TimeRange | None, bool]] = []
    choice_token = uuid4().hex
    preferred_format = preferences.default_format or MediaFormat.VIDEO
    format_order = (
        (preferred_format, MediaFormat.AUDIO)
        if preferred_format is MediaFormat.VIDEO
        else (MediaFormat.AUDIO, MediaFormat.VIDEO)
    )
    if time_range is not None:
        for prefix, selected_range, force_full in (
            ("clip", time_range, False),
            ("full", None, True),
        ):
            choices.extend(
                (
                    f"{prefix}-{media_format.value}:{choice_token}",
                    media_format,
                    selected_range,
                    force_full,
                )
                for media_format in format_order
            )
    else:
        choices = [
            (f"{media_format.value}:{choice_token}", media_format, None, False)
            for media_format in format_order
        ]

    results: list[InlineQueryResultArticle] = []
    for result_id, media_format, selected_range, force_full_title in choices:
        quality_policy = (
            preferences.video_quality
            if media_format is MediaFormat.VIDEO
            else VideoQualityPolicy.AUTO
        )
        _store_pending_url(
            context,
            result_id,
            url,
            custom_caption=custom_caption,
            time_range=selected_range,
            media_format=media_format,
            quality_policy=quality_policy,
            preferences=preferences,
            owner_user_id=user_id,
        )
        cache_quality = _cache_quality_policy(media_format, quality_policy)
        cached = (
            await _cache(context).get_preferred_video(
                url, time_range=selected_range, quality_policy=cache_quality
            )
            if media_format is MediaFormat.VIDEO
            else await _cache(context).get(
                url,
                time_range=selected_range,
                media_format=media_format,
                quality_policy=cache_quality,
            )
        )
        results.append(
            _pending_media_article(
                result_id,
                url,
                logo_base_url=settings.platform_logo_base_url,
                preview=preview,
                cached=cached,
                time_range=selected_range,
                media_format=media_format,
                quality_policy=quality_policy,
                force_full_title=force_full_title,
            )
        )
    await _answer_inline_query(
        query,
        results=results,
        cache_time=1,
        is_personal=True,
    )


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download after the user picks a result, then edit the inline message."""
    chosen = update.chosen_inline_result
    if chosen is None:
        return

    user_id = chosen.from_user.id if chosen.from_user else None
    if not _is_user_allowed(context, user_id):
        return

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logger.warning(
            "Chosen inline result without inline_message_id (enable BotFather /setinlinefeedback)."
        )
        return

    # Evict chosen item from pending map immediately to prevent memory leak
    pending = _pending_map(context).get(chosen.result_id)
    url: str | None
    custom_caption: str | None
    time_range: TimeRange | None
    media_format: MediaFormat
    quality_policy: VideoQualityPolicy
    preferences: UserSharingSettings
    if pending is not None:
        if pending.owner_user_id is not None and pending.owner_user_id != user_id:
            await _edit_inline_text(
                context,
                inline_message_id,
                strings.ACCESS_DENIED,
            )
            return
        _pending_map(context).pop(chosen.result_id, None)
        url = pending.url
        custom_caption = pending.custom_caption
        time_range = pending.time_range
        media_format = pending.media_format
        quality_policy = pending.quality_policy
        preferences = pending.preferences
    else:
        request = extract_media_request(chosen.query or "")
        url = request.url
        custom_caption = request.custom_caption
        preferences = await _user_preferences(context, user_id)
        result_id = chosen.result_id
        media_format = (
            MediaFormat.AUDIO
            if result_id.startswith(("audio:", "clip-audio:", "full-audio:"))
            else MediaFormat.VIDEO
        )
        quality_policy = (
            preferences.video_quality
            if media_format is MediaFormat.VIDEO
            else VideoQualityPolicy.AUTO
        )
        if result_id.startswith("full-"):
            time_range = None
        elif result_id.startswith("clip-"):
            time_range = request.time_range
        else:
            time_range = None
    if url is None:
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_NO_URL,
        )
        return

    display_url = safe_url_for_log(url)
    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        await _edit_inline_text(
            context,
            inline_message_id,
            denial,
        )
        return

    logger.info("Chosen inline result; preparing media for %s", display_url)
    tasks = _task_map(context)
    existing = tasks.get(inline_message_id)
    if existing is not None and not existing.done():
        await _release_user_download_slot(context, user_id)
        return

    task = asyncio.create_task(
        _prepare_inline_media(
            context,
            inline_message_id=inline_message_id,
            url=url,
            result_id=chosen.result_id,
            user_id=user_id,
            custom_caption=custom_caption,
            time_range=time_range,
            media_format=media_format,
            quality_policy=quality_policy,
            preferences=preferences,
        ),
        name=f"prepare-inline-{chosen.result_id}",
    )
    tasks[inline_message_id] = task


async def retry_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry a transient inline failure or switch a failed clip to its full video."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_id = query.from_user.id if query.from_user else None
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return

    retrying = query.data.startswith(_RETRY_PREFIX)
    using_full_video = query.data.startswith(_FULL_FALLBACK_PREFIX)
    if not retrying and not using_full_video:
        await query.answer()
        return

    prefix = _RETRY_PREFIX if retrying else _FULL_FALLBACK_PREFIX
    result_id = query.data.removeprefix(prefix)
    pending = _pending_map(context).get(result_id)
    if pending is None:
        await query.answer(text=strings.INLINE_RETRY_EXPIRED, show_alert=True)
        return
    if pending.owner_user_id is not None and pending.owner_user_id != user_id:
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        await query.answer(text=strings.INLINE_RETRY_EXPIRED, show_alert=True)
        return

    tasks = _task_map(context)
    existing = tasks.get(inline_message_id)
    if existing is not None and not existing.done():
        await query.answer(text=strings.INLINE_ALREADY_PREPARING)
        return

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        await query.answer(text=denial, show_alert=True)
        return

    time_range = None if using_full_video else pending.time_range
    _store_pending_url(
        context,
        result_id,
        pending.url,
        custom_caption=pending.custom_caption,
        time_range=time_range,
        media_format=pending.media_format,
        quality_policy=pending.quality_policy,
        preferences=pending.preferences,
        owner_user_id=pending.owner_user_id,
    )
    await query.answer(text=strings.INLINE_RETRY_ANSWER)
    display_url = safe_url_for_log(pending.url)
    pending_message = (
        strings.INLINE_PENDING_CLIP_MESSAGE.format(
            range_label=format_time_range(time_range),
            url=display_url,
        )
        if time_range is not None
        else strings.INLINE_PENDING_MESSAGE.format(url=display_url)
    )
    await _edit_inline_text(
        context,
        inline_message_id,
        pending_message,
        reply_markup=_cancel_keyboard(result_id),
    )
    task = asyncio.create_task(
        _prepare_inline_media(
            context,
            inline_message_id=inline_message_id,
            url=pending.url,
            result_id=result_id,
            user_id=user_id,
            custom_caption=pending.custom_caption,
            time_range=time_range,
            media_format=pending.media_format,
            quality_policy=pending.quality_policy,
            preferences=pending.preferences,
        ),
        name=f"retry-inline-{result_id}",
    )
    tasks[inline_message_id] = task


async def direct_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply a private-chat Video or Audio choice to the pending link."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    user_id = query.from_user.id if query.from_user else None
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return
    choices = (
        (_VIDEO_BEST_CALLBACK_PREFIX, MediaFormat.VIDEO, VideoQualityPolicy.BEST),
        (_VIDEO_BALANCED_CALLBACK_PREFIX, MediaFormat.VIDEO, VideoQualityPolicy.BALANCED),
        (_VIDEO_CALLBACK_PREFIX, MediaFormat.VIDEO, None),
        (_AUDIO_CALLBACK_PREFIX, MediaFormat.AUDIO, VideoQualityPolicy.AUTO),
    )
    selected = next((choice for choice in choices if query.data.startswith(choice[0])), None)
    if selected is None:
        await query.answer()
        return
    prefix, media_format, quality_override = selected
    choice_id = query.data.removeprefix(prefix)
    pending = _pending_clip_map(context).pop(choice_id, None)
    if pending is None:
        await query.answer(text=strings.DIRECT_CLIP_EXPIRED, show_alert=True)
        return
    if pending.owner_user_id is not None and pending.owner_user_id != user_id:
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return
    quality_policy = quality_override or pending.preferences.video_quality
    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    await query.answer(text=strings.DIRECT_CLIP_CHOICE_ANSWER)
    await msg.edit_text(strings.DIRECT_DOWNLOADING, reply_markup=None)
    await _run_direct_download(
        context,
        chat_id=pending.chat_id,
        user_id=user_id,
        url=pending.url,
        custom_caption=pending.custom_caption,
        time_range=pending.time_range,
        status_message=msg,
        media_format=media_format,
        quality_policy=quality_policy,
        preferences=pending.preferences,
    )


async def clip_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private-chat clip vs full-video choice after a YouTube range was detected."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_id = query.from_user.id if query.from_user else None
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return

    data = query.data
    choices = (
        (_CLIP_BEST_CALLBACK_PREFIX, True, MediaFormat.VIDEO, VideoQualityPolicy.BEST),
        (_CLIP_BALANCED_CALLBACK_PREFIX, True, MediaFormat.VIDEO, VideoQualityPolicy.BALANCED),
        (_CLIP_CALLBACK_PREFIX, True, MediaFormat.VIDEO, None),
        (_CLIP_AUDIO_CALLBACK_PREFIX, True, MediaFormat.AUDIO, VideoQualityPolicy.AUTO),
        (_FULL_BEST_CALLBACK_PREFIX, False, MediaFormat.VIDEO, VideoQualityPolicy.BEST),
        (_FULL_BALANCED_CALLBACK_PREFIX, False, MediaFormat.VIDEO, VideoQualityPolicy.BALANCED),
        (_FULL_CALLBACK_PREFIX, False, MediaFormat.VIDEO, None),
        (_FULL_AUDIO_CALLBACK_PREFIX, False, MediaFormat.AUDIO, VideoQualityPolicy.AUTO),
    )
    selected = next((choice for choice in choices if data.startswith(choice[0])), None)
    if selected is None:
        await query.answer()
        return
    prefix, want_clip, media_format, quality_override = selected
    choice_id = data.removeprefix(prefix)
    pending = _pending_clip_map(context).pop(choice_id, None)
    if pending is None:
        await query.answer(text=strings.DIRECT_CLIP_EXPIRED, show_alert=True)
        msg = query.message
        if isinstance(msg, Message):
            with contextlib.suppress(TelegramError):
                await msg.edit_reply_markup(reply_markup=None)
        return
    if pending.owner_user_id is not None and pending.owner_user_id != user_id:
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return
    quality_policy = quality_override or pending.preferences.video_quality

    await query.answer(text=strings.DIRECT_CLIP_CHOICE_ANSWER)
    time_range = pending.time_range if want_clip else None
    if (
        want_clip
        and time_range is not None
        and time_range.duration_seconds is not None
        and time_range.duration_seconds > MAX_CLIP_SECONDS
    ):
        msg = query.message
        if isinstance(msg, Message):
            await msg.edit_text(
                strings.DOWNLOAD_CLIP_TOO_LONG.format(max_minutes=MAX_CLIP_SECONDS // 60),
                reply_markup=None,
            )
        return

    msg = query.message
    if not isinstance(msg, Message):
        return
    await msg.edit_text(
        strings.DIRECT_DOWNLOADING,
        reply_markup=None,
    )

    await _run_direct_download(
        context,
        chat_id=pending.chat_id,
        user_id=user_id,
        url=pending.url,
        custom_caption=pending.custom_caption,
        time_range=time_range,
        status_message=msg,
        media_format=media_format,
        quality_policy=quality_policy,
        preferences=pending.preferences,
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel prepare and clear the inline placeholder immediately."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    if not _is_user_allowed(context, query.from_user.id if query.from_user else None):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return

    if not query.data.startswith(_CALLBACK_PREFIX):
        await query.answer()
        return

    result_id = query.data.removeprefix(_CALLBACK_PREFIX)
    inline_message_id = query.inline_message_id
    pending = _pending_map(context).get(result_id)
    if (
        pending is not None
        and pending.owner_user_id is not None
        and pending.owner_user_id != (query.from_user.id if query.from_user else None)
    ):
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return
    await query.answer(text=strings.INLINE_CANCEL_ANSWER)

    if inline_message_id:
        _record_cancelled_inline(context, inline_message_id)
        task = _task_map(context).pop(inline_message_id, None)
        if task is not None and not task.done():
            task.cancel()
        # Bots cannot deleteMessage by inline_message_id; clear content instead.
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CANCELLED,
            reply_markup=_EMPTY_KEYBOARD,
        )
        logger.info("Cancelled inline prepare for message %s", inline_message_id)

    _pending_map(context).pop(result_id, None)


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
) -> None:
    settings = _settings(context)
    if preferences is None:
        preferences = await _user_preferences(context, user_id)
    cache = _cache(context)
    cache_quality = _cache_quality_policy(media_format, quality_policy)
    display_url = safe_url_for_log(url)
    media: DownloadedMedia | None = None
    keep_pending_for_retry = False
    started_at = time.monotonic()

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
            logger.info("Cache hit for inline media: %s", display_url)
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
                logger.info(
                    "Inline media sent from cache for %s (%s)",
                    display_url,
                    cached.kind.value,
                )
                return
            except BadRequest as exc:
                logger.warning(
                    "Cached file_id invalid in inline edit for %s, evicting and falling back: %s",
                    display_url,
                    exc.message,
                )
                await cache.evict_entry(cached)
            except Exception:
                logger.exception(
                    "Failed editing inline media from cache for %s, falling back",
                    display_url,
                )
                await cache.evict_entry(cached)

        # 2. Cache miss or fallback to download pipeline
        if inline_message_id in _cancelled_set(context):
            return
        # The chosen article already contains the checking status and Cancel button.
        # Try direct URL import via Telegram first (fastest, zero local upload bandwidth).
        # Skip for clips — that path would send the whole video.
        if time_range is None and media_format is MediaFormat.AUDIO:
            direct_stream = await get_direct_stream(
                url=url,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=min(15, settings.download_timeout_seconds),
                allowed_hosts=settings.allowed_media_hosts,
                media_format=media_format,
                https_only=settings.https_only,
            )
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
                    logger.info(
                        "Inline media ready via direct URL for %s (%s)",
                        display_url,
                        kind.value,
                    )
                    return

        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_DOWNLOADING.format(url=display_url),
            reply_markup=_cancel_keyboard(result_id),
        )
        queue_started_at = time.monotonic()
        loop = asyncio.get_running_loop()

        def show_optimization_status() -> None:
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

        async with _download_slot(context):
            if inline_message_id in _cancelled_set(context):
                return
            if time.monotonic() - queue_started_at >= 0.1:
                logger.info(
                    "Inline download waited %.1fs for a slot: %s",
                    time.monotonic() - queue_started_at,
                    display_url,
                )
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
            )
            logger.info(
                "Inline download completed in %.1fs for %s",
                time.monotonic() - download_started_at,
                display_url,
            )
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
            logger.info(
                "Inline Telegram upload completed in %.1fs for %s",
                time.monotonic() - upload_started_at,
                display_url,
            )
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
        logger.info(
            "Inline media ready for %s (%s) in %.1fs",
            display_url,
            kind.value,
            time.monotonic() - started_at,
        )
    except asyncio.CancelledError:
        logger.info("Inline prepare cancelled for %s", display_url)
        raise
    except DownloadError as exc:
        if inline_message_id in _cancelled_set(context):
            return
        logger.warning("Inline download failed for %s: %s", display_url, exc)
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
    except (NetworkError, TimedOut) as exc:
        if inline_message_id in _cancelled_set(context):
            return
        logger.warning("Inline upload failed for %s: %s", display_url, exc)
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
        logger.exception("Inline prepare failed for %s", display_url)
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_PREPARE_FAILED,
            reply_markup=_EMPTY_KEYBOARD,
        )
    finally:
        _task_map(context).pop(inline_message_id, None)
        _cancelled_set(context).discard(inline_message_id)
        if not keep_pending_for_retry:
            _pending_map(context).pop(result_id, None)
        if media is not None:
            cleanup_media(media)
        await _release_user_download_slot(context, user_id)


def _cancel_keyboard(result_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    strings.INLINE_CANCEL_BUTTON,
                    callback_data=f"{_CALLBACK_PREFIX}{result_id}",
                )
            ]
        ]
    )


def _retry_keyboard(
    result_id: str,
    time_range: TimeRange | None,
    media_format: MediaFormat = MediaFormat.VIDEO,
) -> InlineKeyboardMarkup:
    retry_label = (
        strings.INLINE_RETRY_CLIP_BUTTON if time_range is not None else strings.INLINE_RETRY_BUTTON
    )
    buttons = [
        InlineKeyboardButton(
            retry_label,
            callback_data=f"{_RETRY_PREFIX}{result_id}",
        )
    ]
    if time_range is not None:
        buttons.append(
            InlineKeyboardButton(
                (
                    strings.INLINE_PENDING_FULL_AUDIO_TITLE
                    if media_format is MediaFormat.AUDIO
                    else strings.INLINE_SEND_FULL_BUTTON
                ),
                callback_data=f"{_FULL_FALLBACK_PREFIX}{result_id}",
            )
        )
    return InlineKeyboardMarkup([buttons])


def _error_article(title: str, description: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title=title[:64],
        description=description[:120],
        input_message_content=InputTextMessageContent(
            message_text=f"{title}\n\n{description}"[:4096]
        ),
    )


def _inline_source_details(url: str, cached: CachedMedia | None) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if (
        host == "youtu.be"
        or host in {"youtube.com", "youtube-nocookie.com"}
        or (host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com"))
    ):
        platform, media_type = "YouTube", "video"
    elif host == "tiktok.com" or host.endswith(".tiktok.com"):
        platform = "TikTok"
        if "/video/" in parsed.path:
            media_type = "video"
        elif "/photo/" in parsed.path:
            media_type = "photo"
        else:
            media_type = "media"
    elif host == "instagram.com" or host.endswith(".instagram.com"):
        platform = "Instagram"
        media_type = "video" if "/reel/" in parsed.path else "media"
    elif host in {"x.com", "twitter.com", "vxtwitter.com", "fxtwitter.com", "fixupx.com"}:
        platform, media_type = "X", "media"
    elif host in {"reddit.com", "redd.it", "v.redd.it"} or host.endswith(".reddit.com"):
        platform, media_type = "Reddit", "video" if host == "v.redd.it" else "media"
    elif host in {"facebook.com", "fb.watch"} or host.endswith(".facebook.com"):
        platform = "Facebook"
        media_type = (
            "video"
            if "/reel/" in parsed.path or "/videos/" in parsed.path or host == "fb.watch"
            else "media"
        )
    else:
        platform, media_type = host or "Link", "media"
    if cached is not None:
        if cached.kind is MediaKind.VIDEO:
            media_type = "video"
        elif cached.kind is MediaKind.AUDIO:
            media_type = "audio"
        else:
            media_type = "media"
    return platform, media_type


def _format_media_duration(duration: int) -> str:
    """Format a known media duration without presenting it as a clip range."""
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _pending_media_article(
    result_id: str,
    url: str,
    *,
    logo_base_url: str | None,
    preview: platform_previews.Preview | None = None,
    cached: CachedMedia | None = None,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    force_full_title: bool = False,
) -> InlineQueryResultArticle:
    display_url = safe_url_for_log(url)
    platform, media_type = _inline_source_details(url, cached)
    media_type = "audio" if media_format is MediaFormat.AUDIO else "video"
    details = [platform, media_type.capitalize()]
    if media_format is MediaFormat.VIDEO:
        details.append(
            "Q: "
            + {
                VideoQualityPolicy.AUTO: strings.INLINE_QUALITY_AUTO_DETAIL,
                VideoQualityPolicy.BEST: strings.INLINE_QUALITY_BEST_DETAIL,
                VideoQualityPolicy.BALANCED: strings.INLINE_QUALITY_BALANCED_DETAIL,
            }[quality_policy]
        )
    if force_full_title:
        title = (
            strings.INLINE_PENDING_FULL_AUDIO_TITLE
            if media_format is MediaFormat.AUDIO
            else strings.INLINE_PENDING_FULL_TITLE
        )
        message_text = strings.INLINE_PENDING_MESSAGE.format(url=display_url)
    elif time_range is not None:
        range_label = format_time_range(time_range)
        title = (
            strings.INLINE_PENDING_AUDIO_CLIP_TITLE.format(range_label=range_label)
            if media_format is MediaFormat.AUDIO
            else strings.INLINE_PENDING_VIDEO_CLIP_TITLE.format(range_label=range_label)
        )
        message_text = strings.INLINE_PENDING_CLIP_MESSAGE.format(
            range_label=range_label, url=display_url
        )
    else:
        title = (
            strings.INLINE_PENDING_AUDIO_TITLE
            if media_format is MediaFormat.AUDIO
            else strings.INLINE_PENDING_VIDEO_TITLE
        )
        message_text = strings.INLINE_PENDING_MESSAGE.format(url=display_url)
    details.append("Cached" if cached is not None else "Download")
    thumbnail_url = preview.url if preview else platform_icons.thumbnail_url(url, logo_base_url)
    if preview:
        thumbnail_width, thumbnail_height = preview.width, preview.height
    elif thumbnail_url:
        thumbnail_width, thumbnail_height = 224, 224
    else:
        thumbnail_width, thumbnail_height = None, None
    return InlineQueryResultArticle(
        id=result_id,
        title=title[:64],
        description=" · ".join(details)[:120],
        thumbnail_url=thumbnail_url,
        thumbnail_width=thumbnail_width,
        thumbnail_height=thumbnail_height,
        input_message_content=InputTextMessageContent(message_text=message_text[:4096]),
        # Keyboard is required so Telegram gives us inline_message_id on choose.
        reply_markup=_cancel_keyboard(result_id),
    )


async def _edit_inline_text(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await context.bot.edit_message_text(
            text=text[:4096],
            inline_message_id=inline_message_id,
            reply_markup=reply_markup if reply_markup is not None else _EMPTY_KEYBOARD,
        )
    except TelegramError as exc:
        logger.warning(
            "Could not edit inline message %s: %s",
            inline_message_id,
            exc,
        )


def _input_media(
    file_id: str,
    title: str,
    kind: MediaKind,
    *,
    caption: str | None,
) -> InputMediaVideo | InputMediaAudio | InputMediaDocument:
    # Captions are always plain text (no ParseMode) to avoid injection.
    if kind is MediaKind.VIDEO:
        return InputMediaVideo(media=file_id, caption=caption)
    if kind is MediaKind.AUDIO:
        return InputMediaAudio(media=file_id, caption=caption, title=title)
    return InputMediaDocument(media=file_id, caption=caption)


def _storage_upload_caption(settings: Settings, media_title: str) -> str | None:
    """Caption for the temporary storage-chat upload only.

    Never uses user-supplied custom captions (avoids leaking them into
    STORAGE_CHAT_ID). Media titles are kept only in ``media`` mode.
    """
    if settings.caption_mode is CaptionMode.MEDIA:
        return resolve_caption(CaptionMode.MEDIA, media_title=media_title, custom_caption=None)
    return None


async def _upload_direct_url_for_file_id(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    stream: DirectMediaStream,
) -> tuple[str, str, MediaKind] | None:
    caption = _storage_upload_caption(settings, stream.title)
    try:
        if stream.kind is MediaKind.VIDEO:
            sent_msg = await context.bot.send_video(
                chat_id=settings.storage_chat_id,
                video=stream.direct_url,
                caption=caption,
                duration=stream.duration,
                supports_streaming=True,
                disable_notification=True,
            )
        elif stream.kind is MediaKind.AUDIO:
            sent_msg = await context.bot.send_audio(
                chat_id=settings.storage_chat_id,
                audio=stream.direct_url,
                caption=caption,
                duration=stream.duration,
                title=stream.title,
                disable_notification=True,
            )
        else:
            sent_msg = await context.bot.send_document(
                chat_id=settings.storage_chat_id,
                document=stream.direct_url,
                caption=caption,
                disable_notification=True,
            )
        file_id, result_kind = _file_id_and_kind_from_message(sent_msg)
        if settings.delete_storage_messages:
            try:
                await context.bot.delete_message(
                    chat_id=settings.storage_chat_id,
                    message_id=sent_msg.message_id,
                )
            except TelegramError:
                logger.debug("Could not delete storage message %s", sent_msg.message_id)
        return file_id, stream.title, result_kind
    except TelegramError as exc:
        logger.info(
            "Telegram direct URL fetch not supported or failed for %s: %s",
            safe_url_for_log(stream.direct_url),
            exc,
        )
        return None


async def _upload_for_file_id(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    media: DownloadedMedia,
) -> tuple[str, str, MediaKind, int | None]:
    message = await _send_media_to_chat(
        context,
        settings.storage_chat_id,
        media,
        settings=settings,
        # Storage path: never apply custom captions.
        custom_caption=None,
        force_storage_caption=True,
    )
    file_id, result_kind = _file_id_and_kind_from_message(message)
    if settings.delete_storage_messages:
        try:
            await context.bot.delete_message(
                chat_id=settings.storage_chat_id,
                message_id=message.message_id,
            )
        except TelegramError:
            logger.debug("Could not delete storage message %s", message.message_id)
    return file_id, media.title, result_kind, _message_video_height(message)


async def _send_media_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    media: DownloadedMedia,
    settings: Settings | None = None,
    *,
    custom_caption: str | None = None,
    preferences: UserSharingSettings | None = None,
    original_url: str | None = None,
    force_storage_caption: bool = False,
) -> Message:
    effective_settings = settings if settings is not None else _settings(context)
    timeout = float(
        getattr(
            effective_settings,
            "upload_timeout_seconds",
            DEFAULT_UPLOAD_TIMEOUT_SECONDS,
        )
    )
    path = media.path
    if force_storage_caption:
        caption = _storage_upload_caption(effective_settings, media.title)
    else:
        if preferences is None:
            preferences = UserSharingSettings()
        caption = _resolve_user_caption(
            preferences,
            effective_settings,
            media_title=media.title,
            original_url=original_url or "",
            custom_caption=custom_caption,
        )

    last_error: NetworkError | None = None
    for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
        try:
            with path.open("rb") as file_obj:
                upload = InputFile(file_obj, filename=path.name)
                if media.kind is MediaKind.VIDEO:
                    return await context.bot.send_video(
                        chat_id=chat_id,
                        video=upload,
                        caption=caption,
                        duration=media.duration,
                        supports_streaming=True,
                        disable_notification=True,
                        read_timeout=timeout,
                        write_timeout=timeout,
                    )
                if media.kind is MediaKind.AUDIO:
                    return await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=upload,
                        caption=caption,
                        duration=media.duration,
                        title=media.title,
                        disable_notification=True,
                        read_timeout=timeout,
                        write_timeout=timeout,
                    )
                return await context.bot.send_document(
                    chat_id=chat_id,
                    document=upload,
                    caption=caption,
                    disable_notification=True,
                    read_timeout=timeout,
                    write_timeout=timeout,
                )
        except NetworkError as exc:
            last_error = exc
            if attempt >= _UPLOAD_MAX_ATTEMPTS:
                break
            delay = _UPLOAD_RETRY_BASE_DELAY_SECONDS * attempt
            logger.warning(
                "Telegram upload attempt %s/%s failed (%s); retrying in %.1fs",
                attempt,
                _UPLOAD_MAX_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


async def _send_cached_media_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    cached: CachedMedia,
    *,
    custom_caption: str | None = None,
    preferences: UserSharingSettings | None = None,
    original_url: str | None = None,
) -> Message:
    settings = _settings(context)
    if preferences is None:
        preferences = UserSharingSettings()
    caption = _resolve_user_caption(
        preferences,
        settings,
        media_title=cached.title,
        original_url=original_url or cached.url,
        custom_caption=custom_caption,
    )
    if cached.kind is MediaKind.VIDEO:
        return await context.bot.send_video(
            chat_id=chat_id,
            video=cached.file_id,
            caption=caption,
            duration=cached.duration,
            supports_streaming=True,
            disable_notification=True,
        )
    if cached.kind is MediaKind.AUDIO:
        return await context.bot.send_audio(
            chat_id=chat_id,
            audio=cached.file_id,
            caption=caption,
            duration=cached.duration,
            title=cached.title,
            disable_notification=True,
        )
    return await context.bot.send_document(
        chat_id=chat_id,
        document=cached.file_id,
        caption=caption,
        disable_notification=True,
    )


def _file_id_and_kind_from_message(message: Message) -> tuple[str, MediaKind]:
    if message.video is not None:
        return message.video.file_id, MediaKind.VIDEO
    if message.audio is not None:
        return message.audio.file_id, MediaKind.AUDIO
    if message.document is not None:
        return message.document.file_id, MediaKind.DOCUMENT
    raise RuntimeError(strings.TELEGRAM_NO_FILE_ID)


def _message_video_height(message: Message) -> int | None:
    video = message.video
    height = getattr(video, "height", None) if video is not None else None
    return height if isinstance(height, int) and height > 0 else None
