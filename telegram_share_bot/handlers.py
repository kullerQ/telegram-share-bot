"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
    MediaKind,
    TimeRange,
    cleanup_media,
    download_media,
    extract_media_request,
    format_time_range,
    get_direct_stream,
    is_allowed_media_host,
    is_https_url,
    resolve_caption,
)
from telegram_share_bot.normalizer import safe_url_for_log

logger = logging.getLogger(__name__)

_STALE_INLINE_QUERY_MARKERS = (
    "query is too old",
    "query id is invalid",
)

_CALLBACK_PREFIX = "cancel:"
_RETRY_PREFIX = "retry:"
_FULL_FALLBACK_PREFIX = "fallback:"
_CLIP_CALLBACK_PREFIX = "clip:"
_FULL_CALLBACK_PREFIX = "full:"
_PENDING_KEY = "pending_inline"
_PENDING_CLIP_KEY = "pending_clip_choice"
_TASKS_KEY = "inline_prepare_tasks"
_CANCELLED_KEY = "cancelled_inline"
_USER_DOWNLOADS_KEY = "user_download_counts"
_USER_DOWNLOADS_LOCK_KEY = "user_download_lock"
_USER_COOLDOWN_KEY = "user_download_cooldowns"
_EMPTY_KEYBOARD = InlineKeyboardMarkup([])

_MAX_PENDING_INLINE = 1000
_MAX_PENDING_CLIP = 500
_MAX_CANCELLED_INLINE = 500
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BASE_DELAY_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class PendingInline:
    url: str
    custom_caption: str | None = None
    time_range: TimeRange | None = None


@dataclass(frozen=True, slots=True)
class PendingClipChoice:
    url: str
    custom_caption: str | None
    time_range: TimeRange
    chat_id: int


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


async def _try_acquire_user_download_slot(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> str | None:
    """Reserve a per-user download slot.

    Returns None on success, or a user-facing error string on denial.
    ``max_downloads_per_user == 0`` disables the per-user in-flight cap.
    """
    if user_id is None:
        return strings.ACCESS_DENIED
    settings = _settings(context)
    max_per_user = settings.max_downloads_per_user
    cooldown = settings.download_cooldown_seconds
    now = time.monotonic()
    async with _user_download_lock(context):
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
) -> None:
    pending = _pending_map(context)
    pending[result_id] = PendingInline(
        url=url, custom_caption=custom_caption, time_range=time_range
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


def _clip_choice_keyboard(choice_id: str, time_range: TimeRange) -> InlineKeyboardMarkup:
    range_label = format_time_range(time_range)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    strings.INLINE_PENDING_CLIP_TITLE.format(range_label=range_label),
                    callback_data=f"{_CLIP_CALLBACK_PREFIX}{choice_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    strings.INLINE_PENDING_FULL_TITLE,
                    callback_data=f"{_FULL_CALLBACK_PREFIX}{choice_id}",
                )
            ],
        ]
    )


def _task_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, asyncio.Task[Any]]:
    raw = context.application.bot_data.setdefault(_TASKS_KEY, {})
    return cast(dict[str, asyncio.Task[Any]], raw)


def _cancelled_set(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    raw = context.application.bot_data.setdefault(_CANCELLED_KEY, set())
    return cast(set[str], raw)


def _record_cancelled_inline(
    context: ContextTypes.DEFAULT_TYPE, inline_message_id: str
) -> None:
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

    if not _is_user_allowed(
        context, update.effective_user.id if update.effective_user else None
    ):
        await update.effective_message.reply_text(strings.ACCESS_DENIED)
        return

    bot_name = context.bot.first_name or strings.BOT_DISPLAY_NAME
    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    await update.effective_message.reply_text(
        strings.START_MESSAGE.format(
            bot_name=bot_name, bot_username=bot_username
        ),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    if not _is_user_allowed(
        context, update.effective_user.id if update.effective_user else None
    ):
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
        strings.HELP_MESSAGE.format(
            bot_username=bot_username, caption_help=caption_help
        ),
    )


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

    display_url = safe_url_for_log(url)
    settings = _settings(context)

    if not is_allowed_media_host(url, settings.allowed_media_hosts):
        await message.reply_text(strings.DOWNLOAD_HOST_NOT_ALLOWED)
        return

    if settings.https_only and not is_https_url(url):
        await message.reply_text(strings.DOWNLOAD_HTTPS_REQUIRED)
        return

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
            ),
        )
        await message.reply_text(
            strings.DIRECT_CLIP_PROMPT.format(
                range_label=format_time_range(time_range)
            ),
            reply_markup=_clip_choice_keyboard(choice_id, time_range),
        )
        return

    cache = _cache(context)
    cached = await cache.get(url, time_range=None)
    if cached is not None:
        logger.info("Cache hit for direct URL: %s", display_url)
        try:
            await _send_cached_media_to_chat(
                context,
                message.chat_id,
                cached,
                custom_caption=custom_caption,
            )
            return
        except BadRequest as exc:
            logger.warning(
                "Cached file_id invalid for %s, evicting and falling back to download: %s",
                display_url,
                exc.message,
            )
            await cache.evict(url, time_range=None)
        except Exception:
            logger.exception(
                "Failed sending cached media for %s, falling back", display_url
            )
            await cache.evict(url, time_range=None)

    status = await message.reply_text(strings.DIRECT_PREPARING)
    await _run_direct_download(
        context,
        chat_id=message.chat_id,
        user_id=user_id,
        url=url,
        custom_caption=custom_caption,
        time_range=None,
        status_message=status,
        skip_cache=True,
    )


async def _run_direct_download(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int | None,
    url: str,
    custom_caption: str | None,
    time_range: TimeRange | None,
    status_message: Message,
    skip_cache: bool = False,
) -> None:
    display_url = safe_url_for_log(url)
    cache = _cache(context)
    settings = _settings(context)

    if not skip_cache:
        cached = await cache.get(url, time_range=time_range)
        if cached is not None:
            logger.info("Cache hit for direct URL: %s", display_url)
            try:
                await status_message.edit_text(strings.DIRECT_UPLOADING)
                await _send_cached_media_to_chat(
                    context,
                    chat_id,
                    cached,
                    custom_caption=custom_caption,
                )
                await status_message.edit_text(strings.DIRECT_DONE)
                return
            except BadRequest as exc:
                logger.warning(
                    "Cached file_id invalid for %s, evicting and falling back: %s",
                    display_url,
                    exc.message,
                )
                await cache.evict(url, time_range=time_range)
            except Exception:
                logger.exception(
                    "Failed sending cached media for %s, falling back", display_url
                )
                await cache.evict(url, time_range=time_range)

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        await status_message.edit_text(denial)
        return

    media: DownloadedMedia | None = None
    try:
        # Direct URL import skips clips (would fetch the whole video).
        if time_range is None:
            direct_stream = await get_direct_stream(
                url=url,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=min(15, settings.download_timeout_seconds),
                allowed_hosts=settings.allowed_media_hosts,
                https_only=settings.https_only,
            )
            if direct_stream is not None:
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
                    )
                    await status_message.edit_text(strings.DIRECT_DONE)
                    return

        await status_message.edit_text(strings.DIRECT_DOWNLOADING)
        async with _download_slot(context):
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
            )
            await status_message.edit_text(strings.DIRECT_UPLOADING)
            sent_msg = await _send_media_to_chat(
                context,
                chat_id,
                media,
                settings=settings,
                custom_caption=custom_caption,
            )
        file_id, result_kind = _file_id_and_kind_from_message(sent_msg)
        await cache.set(
            url=url,
            file_id=file_id,
            kind=result_kind,
            title=media.title,
            duration=media.duration,
            time_range=time_range,
        )
        await status_message.edit_text(strings.DIRECT_DONE)
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

    preview = await platform_previews.resolve_preview(url)

    if time_range is not None:
        base_id = uuid4().hex
        clip_id = f"clip:{base_id}"
        full_id = f"full:{base_id}"
        _store_pending_url(
            context,
            clip_id,
            url,
            custom_caption=custom_caption,
            time_range=time_range,
        )
        _store_pending_url(
            context,
            full_id,
            url,
            custom_caption=custom_caption,
            time_range=None,
        )
        clip_cached = await _cache(context).get(url, time_range=time_range)
        full_cached = await _cache(context).get(url, time_range=None)
        await _answer_inline_query(
            query,
            results=[
                _pending_media_article(
                    clip_id,
                    url,
                    logo_base_url=settings.platform_logo_base_url,
                    preview=preview,
                    cached=clip_cached,
                    time_range=time_range,
                ),
                _pending_media_article(
                    full_id,
                    url,
                    logo_base_url=settings.platform_logo_base_url,
                    preview=preview,
                    cached=full_cached,
                    time_range=None,
                    force_full_title=True,
                ),
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    result_id = uuid4().hex
    _store_pending_url(context, result_id, url, custom_caption=custom_caption)
    cached = await _cache(context).get(url)
    await _answer_inline_query(
        query,
        results=[
            _pending_media_article(
                result_id,
                url,
                logo_base_url=settings.platform_logo_base_url,
                preview=preview,
                cached=cached,
            )
        ],
        cache_time=1,
        is_personal=True,
    )


async def chosen_inline_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
            "Chosen inline result without inline_message_id "
            "(enable BotFather /setinlinefeedback)."
        )
        return

    # Evict chosen item from pending map immediately to prevent memory leak
    pending = _pending_map(context).pop(chosen.result_id, None)
    url: str | None
    custom_caption: str | None
    time_range: TimeRange | None
    if pending is not None:
        url = pending.url
        custom_caption = pending.custom_caption
        time_range = pending.time_range
    else:
        request = extract_media_request(chosen.query or "")
        url = request.url
        custom_caption = request.custom_caption
        # result_id prefix decides clip vs full when pending was evicted
        if chosen.result_id.startswith("full:"):
            time_range = None
        elif chosen.result_id.startswith("clip:"):
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
        ),
        name=f"prepare-inline-{chosen.result_id}",
    )
    tasks[inline_message_id] = task


async def retry_inline_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
        ),
        name=f"retry-inline-{result_id}",
    )
    tasks[inline_message_id] = task


async def clip_choice_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Private-chat clip vs full-video choice after a YouTube range was detected."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_id = query.from_user.id if query.from_user else None
    if not _is_user_allowed(context, user_id):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return

    data = query.data
    want_clip = data.startswith(_CLIP_CALLBACK_PREFIX)
    want_full = data.startswith(_FULL_CALLBACK_PREFIX)
    if not want_clip and not want_full:
        await query.answer()
        return

    prefix = _CLIP_CALLBACK_PREFIX if want_clip else _FULL_CALLBACK_PREFIX
    choice_id = data.removeprefix(prefix)
    pending = _pending_clip_map(context).pop(choice_id, None)
    if pending is None:
        await query.answer(text=strings.DIRECT_CLIP_EXPIRED, show_alert=True)
        msg = query.message
        if isinstance(msg, Message):
            with contextlib.suppress(TelegramError):
                await msg.edit_reply_markup(reply_markup=None)
        return

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
                strings.DOWNLOAD_CLIP_TOO_LONG.format(
                    max_minutes=MAX_CLIP_SECONDS // 60
                ),
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
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel prepare and clear the inline placeholder immediately."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    if not _is_user_allowed(
        context, query.from_user.id if query.from_user else None
    ):
        await query.answer(text=strings.ACCESS_DENIED, show_alert=True)
        return

    if not query.data.startswith(_CALLBACK_PREFIX):
        await query.answer()
        return

    result_id = query.data.removeprefix(_CALLBACK_PREFIX)
    inline_message_id = query.inline_message_id
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
) -> None:
    settings = _settings(context)
    cache = _cache(context)
    display_url = safe_url_for_log(url)
    media: DownloadedMedia | None = None
    keep_pending_for_retry = False
    started_at = time.monotonic()

    try:
        # 1. Attempt instant send from cache
        cached = await cache.get(url, time_range=time_range)
        if cached is not None:
            logger.info("Cache hit for inline media: %s", display_url)
            try:
                if inline_message_id in _cancelled_set(context):
                    return
                caption = resolve_caption(
                    settings.caption_mode,
                    media_title=cached.title,
                    custom_caption=custom_caption,
                )
                await context.bot.edit_message_media(
                    media=_input_media(
                        cached.file_id, cached.title, cached.kind, caption=caption
                    ),
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
                    "Cached file_id invalid in inline edit for %s, "
                    "evicting and falling back: %s",
                    display_url,
                    exc.message,
                )
                await cache.evict(url, time_range=time_range)
            except Exception:
                logger.exception(
                    "Failed editing inline media from cache for %s, falling back",
                    display_url,
                )
                await cache.evict(url, time_range=time_range)

        # 2. Cache miss or fallback to download pipeline
        if inline_message_id in _cancelled_set(context):
            return
        # The chosen article already contains the checking status and Cancel button.
        # Try direct URL import via Telegram first (fastest, zero local upload bandwidth).
        # Skip for clips — that path would send the whole video.
        if time_range is None:
            direct_stream = await get_direct_stream(
                url=url,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=min(15, settings.download_timeout_seconds),
                allowed_hosts=settings.allowed_media_hosts,
                https_only=settings.https_only,
            )
            if direct_stream is not None:
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
                    )
                    if inline_message_id in _cancelled_set(context):
                        return
                    caption = resolve_caption(
                        settings.caption_mode,
                        media_title=title,
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
            file_id, title, kind = await _upload_for_file_id(context, settings, media)
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
        )
        if inline_message_id in _cancelled_set(context):
            return
        caption = resolve_caption(
            settings.caption_mode,
            media_title=title,
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
            )
        await _edit_inline_text(
            context,
            inline_message_id,
            str(exc) or strings.INLINE_CHOSEN_DOWNLOAD_FAILED,
            reply_markup=(
                _retry_keyboard(result_id, time_range)
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
        )
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_UPLOAD_FAILED,
            reply_markup=_retry_keyboard(result_id, time_range),
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
    result_id: str, time_range: TimeRange | None
) -> InlineKeyboardMarkup:
    retry_label = (
        strings.INLINE_RETRY_CLIP_BUTTON
        if time_range is not None
        else strings.INLINE_RETRY_BUTTON
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
                strings.INLINE_SEND_FULL_BUTTON,
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
    if host == "youtu.be" or host in {"youtube.com", "youtube-nocookie.com"} or (
        host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")
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


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


def _pending_media_article(
    result_id: str,
    url: str,
    *,
    logo_base_url: str | None,
    preview: platform_previews.Preview | None = None,
    cached: CachedMedia | None = None,
    time_range: TimeRange | None = None,
    force_full_title: bool = False,
) -> InlineQueryResultArticle:
    display_url = safe_url_for_log(url)
    platform, media_type = _inline_source_details(url, cached)
    details = [platform, media_type.capitalize()]
    if force_full_title:
        title = strings.INLINE_PENDING_FULL_TITLE
        message_text = strings.INLINE_PENDING_MESSAGE.format(url=display_url)
    elif time_range is not None:
        range_label = format_time_range(time_range)
        title = strings.INLINE_PENDING_CLIP_TITLE.format(range_label=range_label)
        message_text = strings.INLINE_PENDING_CLIP_MESSAGE.format(
            range_label=range_label, url=display_url
        )
    else:
        title = strings.INLINE_PENDING_TITLE.format(media_type=media_type)
        message_text = strings.INLINE_PENDING_MESSAGE.format(url=display_url)
    duration = (cached.duration if cached is not None else None) or (
        time_range.duration_seconds if time_range is not None else None
    )
    if duration is not None and duration > 0:
        details.append(f"Duration: {_format_duration(duration)}")
    details.append("Instant" if cached is not None else "Download")
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
        input_message_content=InputTextMessageContent(
            message_text=message_text[:4096]
        ),
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
        return resolve_caption(
            CaptionMode.MEDIA, media_title=media_title, custom_caption=None
        )
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
) -> tuple[str, str, MediaKind]:
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
    return file_id, media.title, result_kind


async def _send_media_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    media: DownloadedMedia,
    settings: Settings | None = None,
    *,
    custom_caption: str | None = None,
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
        caption = resolve_caption(
            effective_settings.caption_mode,
            media_title=media.title,
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
) -> Message:
    settings = _settings(context)
    caption = resolve_caption(
        settings.caption_mode,
        media_title=cached.title,
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
