"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import uuid4

from telegram import (
    Message,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.media.models import (
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.requests import (
    MAX_CLIP_SECONDS,
    extract_media_request,
    format_time_range,
)
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.storage.user_settings import (
    UserSharingSettings,
)

from .delivery import _cancel_keyboard, _edit_inline_text
from .direct import _run_direct_download
from .preferences import _user_preferences
from .prepare import _prepare_inline_media
from .state import (
    _AUDIO_CALLBACK_PREFIX,
    _CALLBACK_PREFIX,
    _CLIP_AUDIO_CALLBACK_PREFIX,
    _CLIP_BALANCED_CALLBACK_PREFIX,
    _CLIP_BEST_CALLBACK_PREFIX,
    _CLIP_CALLBACK_PREFIX,
    _DIRECT_CHOICE_CANCEL_PREFIX,
    _EMPTY_KEYBOARD,
    _FULL_AUDIO_CALLBACK_PREFIX,
    _FULL_BALANCED_CALLBACK_PREFIX,
    _FULL_BEST_CALLBACK_PREFIX,
    _FULL_CALLBACK_PREFIX,
    _FULL_FALLBACK_PREFIX,
    _RETRY_PREFIX,
    _VIDEO_BALANCED_CALLBACK_PREFIX,
    _VIDEO_BEST_CALLBACK_PREFIX,
    _VIDEO_CALLBACK_PREFIX,
    _is_user_allowed,
    _pending_clip_map,
    _pending_map,
    _record_cancelled_inline,
    _release_user_download_slot,
    _store_pending_url,
    _task_map,
    _try_acquire_user_download_slot,
)
from .trace import _SendTrace

logger = logging.getLogger("telegram_share_bot.handlers")


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

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        _SendTrace(uuid4().hex[:12], "inline", media_format, time_range, user_id).event(
            "denied", failure="rate_limit"
        )
        await _edit_inline_text(
            context,
            inline_message_id,
            denial,
        )
        return

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


async def private_choice_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dismiss a pending private-chat format or clip choice."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    user_id = query.from_user.id if query.from_user else None
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return
    if not query.data.startswith(_DIRECT_CHOICE_CANCEL_PREFIX):
        await query.answer()
        return
    choice_id = query.data.removeprefix(_DIRECT_CHOICE_CANCEL_PREFIX)
    pending = _pending_clip_map(context).get(choice_id)
    if pending is None:
        await query.answer(text=strings.DIRECT_CLIP_EXPIRED, show_alert=True)
        return
    if pending.owner_user_id is not None and pending.owner_user_id != user_id:
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return
    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    _pending_clip_map(context).pop(choice_id, None)
    await query.answer(text=strings.DIRECT_CANCELLED)
    await msg.edit_text(strings.DIRECT_CANCELLED, reply_markup=None)


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
    pending = _pending_clip_map(context).get(choice_id)
    if pending is None:
        await query.answer(text=strings.DIRECT_CLIP_EXPIRED, show_alert=True)
        return
    if pending.owner_user_id is not None and pending.owner_user_id != user_id:
        await query.answer(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        return
    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    _pending_clip_map(context).pop(choice_id, None)
    quality_policy = quality_override or pending.preferences.video_quality
    await query.answer(text=strings.DIRECT_CLIP_CHOICE_ANSWER)
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
    pending = _pending_clip_map(context).get(choice_id)
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
    msg = query.message
    if not isinstance(msg, Message):
        await query.answer()
        return
    _pending_clip_map(context).pop(choice_id, None)
    quality_policy = quality_override or pending.preferences.video_quality

    await query.answer(text=strings.DIRECT_CLIP_CHOICE_ANSWER)
    time_range = pending.time_range if want_clip else None
    if (
        want_clip
        and time_range is not None
        and time_range.duration_seconds is not None
        and time_range.duration_seconds > MAX_CLIP_SECONDS
    ):
        await msg.edit_text(
            strings.DOWNLOAD_CLIP_TOO_LONG.format(max_minutes=MAX_CLIP_SECONDS // 60),
            reply_markup=None,
        )
        return

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

    _pending_map(context).pop(result_id, None)
