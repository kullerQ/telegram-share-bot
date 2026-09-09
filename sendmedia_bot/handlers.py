"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import logging
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

from sendmedia_bot import strings
from sendmedia_bot.config import Settings
from sendmedia_bot.downloader import (
    DownloadedMedia,
    DownloadError,
    MediaKind,
    cleanup_media,
    download_media,
    extract_url,
)

logger = logging.getLogger(__name__)

_STALE_INLINE_QUERY_MARKERS = (
    "query is too old",
    "query id is invalid",
)

_CALLBACK_PREFIX = "cancel:"
_PENDING_KEY = "pending_inline"
_TASKS_KEY = "inline_prepare_tasks"
_CANCELLED_KEY = "cancelled_inline"
_EMPTY_KEYBOARD = InlineKeyboardMarkup([])


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    settings = context.application.bot_data.get("settings")
    if not isinstance(settings, Settings):
        raise TypeError("Settings were not attached to the application.")
    return settings


def _pending_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    raw = context.application.bot_data.setdefault(_PENDING_KEY, {})
    return cast(dict[str, str], raw)


def _task_map(context: ContextTypes.DEFAULT_TYPE) -> dict[str, asyncio.Task[Any]]:
    raw = context.application.bot_data.setdefault(_TASKS_KEY, {})
    return cast(dict[str, asyncio.Task[Any]], raw)


def _cancelled_set(context: ContextTypes.DEFAULT_TYPE) -> set[str]:
    raw = context.application.bot_data.setdefault(_CANCELLED_KEY, set())
    return cast(set[str], raw)


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

    bot_username = context.bot.username or strings.FALLBACK_BOT_USERNAME
    chat_id = update.effective_chat.id
    text = strings.START_MESSAGE.format(
        bot_name=strings.BOT_DISPLAY_NAME,
        bot_username=bot_username,
        example_url=strings.EXAMPLE_MEDIA_URL,
        chat_id=chat_id,
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback: download when a URL is sent directly to the bot in private chat."""
    message = update.effective_message
    if message is None or not message.text:
        return

    url = extract_url(message.text)
    if url is None:
        await message.reply_text(strings.DIRECT_URL_HINT)
        return

    status = await message.reply_text(strings.DIRECT_DOWNLOADING)
    settings = _settings(context)
    media: DownloadedMedia | None = None
    try:
        media = await download_media(
            url=url,
            download_dir=settings.download_dir,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=settings.download_timeout_seconds,
        )
        await _send_media_to_chat(context, message.chat_id, media)
        await status.edit_text(strings.DIRECT_DONE)
    except DownloadError as exc:
        await status.edit_text(strings.DIRECT_DOWNLOAD_FAILED.format(error=exc))
    except Exception:
        logger.exception("Failed to handle direct URL message")
        await status.edit_text(strings.DIRECT_SEND_FAILED)
    finally:
        if media is not None:
            cleanup_media(media)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer quickly; heavy download happens after the result is chosen."""
    query = update.inline_query
    if query is None:
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

    url = extract_url(text)
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

    result_id = uuid4().hex
    _pending_map(context)[result_id] = url
    await _answer_inline_query(
        query,
        results=[_pending_media_article(result_id, url)],
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

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logger.warning(
            "Chosen inline result without inline_message_id "
            "(enable BotFather /setinlinefeedback)."
        )
        return

    url = _pending_map(context).get(chosen.result_id) or extract_url(chosen.query or "")
    if url is None:
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_NO_URL,
        )
        return

    logger.info("Chosen inline result; preparing media for %s", url)
    tasks = _task_map(context)
    existing = tasks.get(inline_message_id)
    if existing is not None and not existing.done():
        return

    task = asyncio.create_task(
        _prepare_inline_media(
            context,
            inline_message_id=inline_message_id,
            url=url,
            result_id=chosen.result_id,
        ),
        name=f"prepare-inline-{chosen.result_id}",
    )
    tasks[inline_message_id] = task


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel prepare and clear the inline placeholder immediately."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    if not query.data.startswith(_CALLBACK_PREFIX):
        await query.answer()
        return

    result_id = query.data.removeprefix(_CALLBACK_PREFIX)
    inline_message_id = query.inline_message_id
    await query.answer(text=strings.INLINE_CANCEL_ANSWER)

    if inline_message_id:
        _cancelled_set(context).add(inline_message_id)
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
) -> None:
    settings = _settings(context)
    media: DownloadedMedia | None = None
    try:
        if inline_message_id in _cancelled_set(context):
            return
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_DOWNLOADING.format(url=url),
            reply_markup=_cancel_keyboard(result_id),
        )
        media = await download_media(
            url=url,
            download_dir=settings.download_dir,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=settings.download_timeout_seconds,
        )
        if inline_message_id in _cancelled_set(context):
            return
        file_id, title, kind = await _upload_for_file_id(context, settings, media)
        if inline_message_id in _cancelled_set(context):
            return
        await context.bot.edit_message_media(
            media=_input_media(file_id, title, kind),
            inline_message_id=inline_message_id,
            reply_markup=_EMPTY_KEYBOARD,
        )
        logger.info("Inline media ready for %s (%s)", url, kind.value)
    except asyncio.CancelledError:
        logger.info("Inline prepare cancelled for %s", url)
        raise
    except DownloadError as exc:
        if inline_message_id in _cancelled_set(context):
            return
        logger.warning("Inline download failed for %s: %s", url, exc)
        await _edit_inline_text(
            context,
            inline_message_id,
            strings.INLINE_CHOSEN_DOWNLOAD_FAILED.format(error=exc),
            reply_markup=_cancel_keyboard(result_id),
        )
    except Exception:
        if inline_message_id in _cancelled_set(context):
            return
        logger.exception("Inline prepare failed for %s", url)
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


def _pending_media_article(result_id: str, url: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=strings.INLINE_PENDING_TITLE,
        description=url[:120],
        input_message_content=InputTextMessageContent(
            message_text=strings.INLINE_PENDING_MESSAGE.format(url=url)[:4096]
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
) -> InputMediaVideo | InputMediaAudio | InputMediaDocument:
    if kind is MediaKind.VIDEO:
        return InputMediaVideo(media=file_id, caption=title)
    if kind is MediaKind.AUDIO:
        return InputMediaAudio(media=file_id, caption=title, title=title)
    return InputMediaDocument(media=file_id, caption=title)


async def _upload_for_file_id(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    media: DownloadedMedia,
) -> tuple[str, str, MediaKind]:
    message = await _send_media_to_chat(context, settings.storage_chat_id, media)
    file_id, result_kind = _file_id_and_kind_from_message(message)
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
) -> Message:
    path = media.path
    caption = media.title

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
            )
        if media.kind is MediaKind.AUDIO:
            return await context.bot.send_audio(
                chat_id=chat_id,
                audio=upload,
                caption=caption,
                duration=media.duration,
                title=media.title,
                disable_notification=True,
            )
        return await context.bot.send_document(
            chat_id=chat_id,
            document=upload,
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
