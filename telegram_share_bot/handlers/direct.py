"""Direct command and private-chat handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from html import escape
from typing import Any
from uuid import uuid4

from telegram import Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
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
from telegram_share_bot.media.requests import extract_media_request, format_time_range
from telegram_share_bot.media.security import is_allowed_media_host, is_https_url
from telegram_share_bot.media.work import MediaWorkLease
from telegram_share_bot.platforms.urls import has_url_credentials
from telegram_share_bot.storage.media_cache import CachedMedia
from telegram_share_bot.storage.user_settings import CaptionPreference, UserSharingSettings

from .delivery import (
    _file_id_and_kind_from_message,
    _message_video_height,
    _send_cached_media_to_chat,
    _send_media_to_chat,
    _upload_direct_url_for_file_id,
)
from .preferences import (
    _settings_keyboard,
    _settings_text,
    _user_preferences,
    _user_settings_store,
)
from .state import (
    PendingClipChoice,
    UserDownloadLease,
    _acquire_download_slot,
    _cache,
    _cache_quality_policy,
    _clip_choice_keyboard,
    _format_choice_keyboard,
    _is_user_allowed,
    _settings,
    _store_pending_clip_choice,
    _try_acquire_user_download_slot,
)
from .trace import _download_failure_category, _SendTrace

logger = logging.getLogger("telegram_share_bot.handlers")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_chat is None:
        return

    if not _is_user_allowed(context, update.effective_user.id if update.effective_user else None):
        await update.effective_message.reply_text(strings.ACCESS_DENIED)
        return

    bot_name = context.bot.first_name or strings.BOT_DISPLAY_NAME
    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    await update.effective_message.reply_text(
        strings.START_MESSAGE.format(bot_name=escape(bot_name), bot_username=escape(bot_username)),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    if not _is_user_allowed(context, update.effective_user.id if update.effective_user else None):
        await message.reply_text(strings.ACCESS_DENIED)
        return

    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    await message.reply_text(
        strings.HELP_MESSAGE.format(bot_username=escape(bot_username)),
        parse_mode=ParseMode.HTML,
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
        reply_markup=_settings_keyboard(user_id, preferences, _settings(context)),
        parse_mode=ParseMode.HTML,
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
    elif field == "all" and value == "reset":
        preferences = await store.reset(user_id)
    else:
        await query.answer(text=strings.SETTINGS_INVALID_CHOICE, show_alert=True)
        return

    await query.answer(text=strings.SETTINGS_SAVED)
    try:
        await query.edit_message_text(
            _settings_text(preferences, _settings(context)),
            reply_markup=_settings_keyboard(user_id, preferences, _settings(context)),
            parse_mode=ParseMode.HTML,
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
    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    if url is None:
        await message.reply_text(strings.DIRECT_URL_HINT.format(
            bot_username=escape(bot_username)
        ))
        return

    if has_url_credentials(url):
        await message.reply_text(strings.DOWNLOAD_UNSAFE_URL)
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
            reply_markup=_clip_choice_keyboard(
                choice_id, time_range, preferences.video_quality, preferences.default_format
            ),
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
    if has_url_credentials(request.url):
        await message.reply_text(strings.DOWNLOAD_UNSAFE_URL)
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


async def _edit_direct_status(status_message: Message, text: str) -> None:
    """A repeated progress stage is already visible; continue the send."""
    try:
        await status_message.edit_text(text)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


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
    trace = _SendTrace(uuid4().hex[:12], "direct", media_format, time_range, user_id)
    trace.cache_outcome = "bypass" if skip_cache else "miss"
    trace.event("start")
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
            trace.cache_outcome = "hit"
            try:
                await _edit_direct_status(status_message, strings.DIRECT_UPLOADING)
                await _send_cached_media_to_chat(
                    context,
                    chat_id,
                    cached,
                    custom_caption=custom_caption,
                    preferences=preferences,
                    original_url=url,
                )
                await _remove_completed_direct_status(status_message)
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

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        trace.event("denied", failure="rate_limit")
        await _edit_direct_status(status_message, denial)
        return
    assert user_id is not None

    media: DownloadedMedia | None = None
    work_lease: MediaWorkLease | None = None
    user_lease = UserDownloadLease(user_id)

    def register_worker(worker: asyncio.Task[Any]) -> None:
        user_lease.track_worker(context, worker)

    try:
        work_lease = await _acquire_download_slot(context, settings.download_timeout_seconds)
        # Direct URL import skips clips (would fetch the whole video).
        if time_range is None and media_format is MediaFormat.AUDIO:
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
            if direct_stream is not None:
                ensure_full_media_duration(
                    direct_stream.duration, settings.max_media_duration_seconds
                )
                await _edit_direct_status(status_message, strings.DIRECT_UPLOADING)
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
                    trace.cache_outcome = "direct_url"
                    trace.event("complete")
                    return
                trace.event("direct_url_fallback", failure="telegram_fetch")

        await _edit_direct_status(status_message, strings.DIRECT_DOWNLOADING)
        loop = asyncio.get_running_loop()

        def show_optimization_status() -> None:
            trace.event("optimization")
            future = asyncio.run_coroutine_threadsafe(
                _edit_direct_status(status_message, strings.OPTIMIZING_FOR_TELEGRAM), loop
            )
            with contextlib.suppress(Exception):
                future.result(timeout=5)

        download_started_at = time.monotonic()
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
            work_lease=work_lease,
            on_worker_registered=register_worker,
        )
        trace.event("download", stage_started_at=download_started_at)
        await _edit_direct_status(status_message, strings.DIRECT_UPLOADING)
        upload_started_at = time.monotonic()
        sent_msg = await _send_media_to_chat(
            context,
            chat_id,
            media,
            settings=settings,
            custom_caption=custom_caption,
            preferences=preferences,
            original_url=url,
        )
        trace.event("upload", stage_started_at=upload_started_at)
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
        trace.event("complete")
    except DownloadError as exc:
        trace.event("failed", failure=_download_failure_category(exc), level=logging.WARNING)
        await _edit_direct_status(status_message, str(exc) or strings.DIRECT_DOWNLOAD_FAILED)
    except (NetworkError, TimedOut):
        trace.event("failed", failure="upload_network", level=logging.WARNING)
        await _edit_direct_status(status_message, strings.DIRECT_UPLOAD_FAILED)
    except Exception:
        trace.event("failed", failure="unexpected", level=logging.ERROR, exc_info=True)
        await _edit_direct_status(status_message, strings.DIRECT_SEND_FAILED)
    finally:
        try:
            if media is not None:
                await asyncio.to_thread(cleanup_media, media)
        except Exception:
            logger.warning("Direct media cleanup failed category=filesystem")
        finally:
            try:
                if work_lease is not None:
                    await work_lease.release()
            finally:
                await user_lease.release(context)
