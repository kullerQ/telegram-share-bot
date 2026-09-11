"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast
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
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.cache import CachedMedia, MediaCache
from telegram_share_bot.config import (
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    CaptionMode,
    Settings,
)
from telegram_share_bot.downloader import (
    DirectMediaStream,
    DownloadedMedia,
    DownloadError,
    MediaKind,
    cleanup_media,
    download_media,
    extract_url_and_caption,
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
_PENDING_KEY = "pending_inline"
_TASKS_KEY = "inline_prepare_tasks"
_CANCELLED_KEY = "cancelled_inline"
_USER_DOWNLOADS_KEY = "user_download_counts"
_USER_DOWNLOADS_LOCK_KEY = "user_download_lock"
_USER_COOLDOWN_KEY = "user_download_cooldowns"
_EMPTY_KEYBOARD = InlineKeyboardMarkup([])

_MAX_PENDING_INLINE = 1000
_MAX_CANCELLED_INLINE = 500


@dataclass(frozen=True, slots=True)
class PendingInline:
    url: str
    custom_caption: str | None = None


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
) -> None:
    pending = _pending_map(context)
    pending[result_id] = PendingInline(url=url, custom_caption=custom_caption)
    while len(pending) > _MAX_PENDING_INLINE:
        try:
            pending.pop(next(iter(pending)))
        except (KeyError, StopIteration):
            break


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

    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    bot_name = context.bot.first_name or strings.BOT_DISPLAY_NAME
    settings = _settings(context)
    caption_hint = ""
    if settings.caption_mode is CaptionMode.CUSTOM:
        caption_hint = strings.START_CAPTION_CUSTOM_HINT.format(
            bot_username=bot_username,
            example_url=strings.EXAMPLE_MEDIA_URL,
        )
    elif settings.caption_mode is CaptionMode.OFF:
        caption_hint = strings.START_CAPTION_OFF_HINT
    text = strings.START_MESSAGE.format(
        bot_name=bot_name,
        bot_username=bot_username,
        example_url=strings.EXAMPLE_MEDIA_URL,
        caption_hint=caption_hint,
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download or send cached media when a URL is sent directly in private chat."""
    message = update.effective_message
    if message is None or not message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not _is_user_allowed(context, user_id):
        await message.reply_text(strings.ACCESS_DENIED)
        return

    url, custom_caption = extract_url_and_caption(message.text)
    if url is None:
        await message.reply_text(strings.DIRECT_URL_HINT)
        return

    display_url = safe_url_for_log(url)
    cache = _cache(context)
    settings = _settings(context)

    if not is_allowed_media_host(url, settings.allowed_media_hosts):
        await message.reply_text(strings.DOWNLOAD_HOST_NOT_ALLOWED)
        return

    if settings.https_only and not is_https_url(url):
        await message.reply_text(strings.DOWNLOAD_HTTPS_REQUIRED)
        return

    # 1. Attempt instant send from cache
    cached = await cache.get(url)
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
            await cache.evict(url)
        except Exception:
            logger.exception(
                "Failed sending cached media for %s, falling back", display_url
            )
            await cache.evict(url)

    denial = await _try_acquire_user_download_slot(context, user_id)
    if denial is not None:
        await message.reply_text(denial)
        return

    # 2. Try direct URL import via Telegram first
    status = await message.reply_text(strings.DIRECT_DOWNLOADING)
    media: DownloadedMedia | None = None
    try:
        direct_stream = await get_direct_stream(
            url=url,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=min(15, settings.download_timeout_seconds),
            allowed_hosts=settings.allowed_media_hosts,
            https_only=settings.https_only,
        )
        if direct_stream is not None:
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
                )
                await _send_cached_media_to_chat(
                    context,
                    message.chat_id,
                    CachedMedia(
                        url=url,
                        file_id=file_id,
                        kind=kind,
                        title=title,
                        duration=direct_stream.duration,
                    ),
                    custom_caption=custom_caption,
                )
                await status.edit_text(strings.DIRECT_DONE)
                return

        # 3. Fallback to local download and upload
        async with _download_slot(context):
            media = await download_media(
                url=url,
                download_dir=settings.download_dir,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=settings.download_timeout_seconds,
                allowed_hosts=settings.allowed_media_hosts,
                https_only=settings.https_only,
            )
            sent_msg = await _send_media_to_chat(
                context,
                message.chat_id,
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
        )
        await status.edit_text(strings.DIRECT_DONE)
    except DownloadError as exc:
        logger.warning("Direct download failed for %s: %s", display_url, exc)
        await status.edit_text(strings.DIRECT_DOWNLOAD_FAILED)
    except Exception:
        logger.exception("Failed to handle direct URL message")
        await status.edit_text(strings.DIRECT_SEND_FAILED)
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

    url, custom_caption = extract_url_and_caption(text)
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

    result_id = uuid4().hex
    _store_pending_url(context, result_id, url, custom_caption=custom_caption)
    cached = await _cache(context).get(url)
    await _answer_inline_query(
        query,
        results=[_pending_media_article(result_id, url, is_cached=cached is not None)],
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
    if pending is not None:
        url = pending.url
        custom_caption = pending.custom_caption
    else:
        url, custom_caption = extract_url_and_caption(chosen.query or "")
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
        ),
        name=f"prepare-inline-{chosen.result_id}",
    )
    tasks[inline_message_id] = task


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
) -> None:
    settings = _settings(context)
    cache = _cache(context)
    display_url = safe_url_for_log(url)
    media: DownloadedMedia | None = None

    try:
        # 1. Attempt instant send from cache
        cached = await cache.get(url)
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
                await cache.evict(url)
            except Exception:
                logger.exception(
                    "Failed editing inline media from cache for %s, falling back",
                    display_url,
                )
                await cache.evict(url)

        # 2. Cache miss or fallback to download pipeline
        if inline_message_id in _cancelled_set(context):
            return
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_DOWNLOADING.format(url=display_url),
            reply_markup=_cancel_keyboard(result_id),
        )
        # Try direct URL import via Telegram first (fastest, zero local upload bandwidth)
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

        async with _download_slot(context):
            if inline_message_id in _cancelled_set(context):
                return
            media = await download_media(
                url=url,
                download_dir=settings.download_dir,
                max_file_bytes=settings.max_file_bytes,
                timeout_seconds=settings.download_timeout_seconds,
                allowed_hosts=settings.allowed_media_hosts,
                https_only=settings.https_only,
            )
            if inline_message_id in _cancelled_set(context):
                return
            file_id, title, kind = await _upload_for_file_id(context, settings, media)
        await cache.set(
            url=url,
            file_id=file_id,
            kind=kind,
            title=title,
            duration=media.duration,
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
        logger.info("Inline media ready for %s (%s)", display_url, kind.value)
    except asyncio.CancelledError:
        logger.info("Inline prepare cancelled for %s", display_url)
        raise
    except DownloadError as exc:
        if inline_message_id in _cancelled_set(context):
            return
        logger.warning("Inline download failed for %s: %s", display_url, exc)
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_DOWNLOAD_FAILED,
            reply_markup=_cancel_keyboard(result_id),
        )
    except Exception:
        if inline_message_id in _cancelled_set(context):
            return
        logger.exception("Inline prepare failed for %s", display_url)
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_PREPARE_FAILED,
            reply_markup=_cancel_keyboard(result_id),
        )
    finally:
        _task_map(context).pop(inline_message_id, None)
        _cancelled_set(context).discard(inline_message_id)
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


def _error_article(title: str, description: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title=title[:64],
        description=description[:120],
        input_message_content=InputTextMessageContent(
            message_text=f"{title}\n\n{description}"[:4096]
        ),
    )


def _pending_media_article(
    result_id: str, url: str, *, is_cached: bool = False
) -> InlineQueryResultArticle:
    display_url = safe_url_for_log(url)
    title = (
        f"⚡ {strings.INLINE_PENDING_TITLE} (cached)"
        if is_cached
        else strings.INLINE_PENDING_TITLE
    )
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=display_url[:120],
        input_message_content=InputTextMessageContent(
            message_text=strings.INLINE_PENDING_MESSAGE.format(url=display_url)[:4096]
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
