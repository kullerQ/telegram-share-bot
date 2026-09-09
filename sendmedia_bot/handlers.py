"""Telegram command and inline-query handlers."""

from __future__ import annotations

import logging
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

_PREPARING_CALLBACK_DATA = "inline_preparing"
_EMPTY_KEYBOARD = InlineKeyboardMarkup([])


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    settings = context.application.bot_data.get("settings")
    if not isinstance(settings, Settings):
        raise TypeError("Settings were not attached to the application.")
    return settings


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

    bot_username = context.bot.username or "YourBot"
    chat_id = update.effective_chat.id
    text = (
        "SendMedia Bot\n\n"
        "Use me *inline* in any chat:\n"
        f"`@{bot_username} https://example.com/video`\n\n"
        "Tap the result to send a placeholder, then wait while I download "
        "and replace it with the media.\n\n"
        "You can also paste a media URL here and I will send the file back.\n\n"
        f"Your chat id (for `STORAGE_CHAT_ID`): `{chat_id}`\n\n"
        "BotFather setup:\n"
        "• `/setinline` — enable inline mode\n"
        "• `/setinlinefeedback` — required so I can finish the download after you tap"
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
        await message.reply_text("Send a media URL, or use me inline: @BotName <url>")
        return

    status = await message.reply_text("Downloading…")
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
        await status.edit_text("Done.")
    except DownloadError as exc:
        await status.edit_text(f"Could not download: {exc}")
    except Exception:
        logger.exception("Failed to handle direct URL message")
        await status.edit_text("Something went wrong while sending the media.")
    finally:
        if media is not None:
            cleanup_media(media)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer quickly; heavy download happens in chosen_inline_result."""
    query = update.inline_query
    if query is None:
        return

    text = (query.query or "").strip()
    if not text:
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    "Paste a media URL",
                    "Type a YouTube, Twitter/X, etc. link after the bot username.",
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
                    "No URL found",
                    "Include a full http(s) link in the inline query.",
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    await _answer_inline_query(
        query,
        results=[_pending_media_article(url)],
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
            "Chosen inline result without inline_message_id; "
            "enable /setinlinefeedback and ensure results include a keyboard."
        )
        return

    url = extract_url(chosen.query or "")
    if url is None:
        await _edit_inline_text(
            context,
            inline_message_id,
            "No URL found in the query.",
        )
        return

    settings = _settings(context)
    media: DownloadedMedia | None = None
    try:
        await _edit_inline_text(
            context,
            inline_message_id,
            f"Downloading…\n{url}",
            reply_markup=_preparing_keyboard(),
        )
        media = await download_media(
            url=url,
            download_dir=settings.download_dir,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=settings.download_timeout_seconds,
        )
        file_id, title, kind = await _upload_for_file_id(context, settings, media)
        await context.bot.edit_message_media(
            media=_input_media(file_id, title, kind),
            inline_message_id=inline_message_id,
            reply_markup=_EMPTY_KEYBOARD,
        )
    except DownloadError as exc:
        await _edit_inline_text(
            context,
            inline_message_id,
            f"Download failed\n\n{exc}",
        )
    except Exception:
        logger.exception("Chosen inline result failed for %s", url)
        await _edit_inline_text(
            context,
            inline_message_id,
            "Something went wrong while preparing the media.",
        )
    finally:
        if media is not None:
            cleanup_media(media)


async def preparing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer(text="Still preparing…")


def _preparing_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Preparing…", callback_data=_PREPARING_CALLBACK_DATA)]]
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


def _pending_media_article(url: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title="Send media",
        description=url[:120],
        input_message_content=InputTextMessageContent(
            message_text=f"Preparing media…\n{url}"[:4096]
        ),
        # Keyboard is required so Telegram gives us inline_message_id on choose.
        reply_markup=_preparing_keyboard(),
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
    except TelegramError:
        logger.debug("Could not edit inline message %s", inline_message_id)


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
    raise RuntimeError("Telegram did not return a usable file_id.")
