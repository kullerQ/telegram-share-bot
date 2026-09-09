"""Telegram command and inline-query handlers."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from telegram import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedVideo,
    InputFile,
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

InlineResult = (
    InlineQueryResultArticle
    | InlineQueryResultCachedAudio
    | InlineQueryResultCachedDocument
    | InlineQueryResultCachedVideo
)


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
    results: list[InlineResult],
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
        "You can also paste a media URL here and I will send the file back.\n\n"
        f"Your chat id (for `STORAGE_CHAT_ID`): `{chat_id}`\n\n"
        "Enable inline mode with BotFather `/setinline` if results do not appear."
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

    settings = _settings(context)
    media: DownloadedMedia | None = None
    try:
        media = await download_media(
            url=url,
            download_dir=settings.download_dir,
            max_file_bytes=settings.max_file_bytes,
            timeout_seconds=settings.download_timeout_seconds,
        )
        file_id, result_title, result_kind = await _upload_for_file_id(
            context, settings, media
        )
        result = _cached_result(file_id, result_title, result_kind, media.path)
        await _answer_inline_query(
            query,
            results=[result],
            cache_time=30,
            is_personal=True,
        )
    except DownloadError as exc:
        await _answer_inline_query(
            query,
            results=[_error_article("Download failed", str(exc))],
            cache_time=1,
            is_personal=True,
        )
    except Exception as exc:
        logger.exception("Inline query failed for %s", url)
        await _answer_inline_query(
            query,
            results=[_error_article("Upload failed", str(exc))],
            cache_time=1,
            is_personal=True,
        )
    finally:
        if media is not None:
            cleanup_media(media)


def _error_article(title: str, description: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title=title[:64],
        description=description[:120],
        input_message_content=InputTextMessageContent(
            message_text=f"{title}\n\n{description}"[:4096]
        ),
    )


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


def _cached_result(
    file_id: str,
    title: str,
    kind: MediaKind,
    path: Path,
) -> (
    InlineQueryResultCachedVideo
    | InlineQueryResultCachedAudio
    | InlineQueryResultCachedDocument
):
    result_id = str(uuid4())
    if kind is MediaKind.VIDEO:
        return InlineQueryResultCachedVideo(
            id=result_id,
            video_file_id=file_id,
            title=title,
            caption=title,
        )
    if kind is MediaKind.AUDIO:
        return InlineQueryResultCachedAudio(
            id=result_id,
            audio_file_id=file_id,
            caption=title,
        )
    suffix = path.suffix
    return InlineQueryResultCachedDocument(
        id=result_id,
        document_file_id=file_id,
        title=title,
        caption=title,
        description=f"{suffix.lstrip('.').upper()} file" if suffix else "Document",
    )
