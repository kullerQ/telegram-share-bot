"""Telegram command and inline-query handlers."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit
from uuid import uuid4

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
)
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    CaptionMode,
    Settings,
)
from telegram_share_bot.media.captions import resolve_caption
from telegram_share_bot.media.models import (
    DirectMediaStream,
    DownloadedMedia,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.requests import (
    format_time_range,
)
from telegram_share_bot.platforms import icons as platform_icons
from telegram_share_bot.platforms import previews as platform_previews
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.storage.media_cache import CachedMedia
from telegram_share_bot.storage.user_settings import (
    UserSharingSettings,
)

from .preferences import _RenderedCaption, _resolve_user_caption
from .state import (
    _CALLBACK_PREFIX,
    _EMPTY_KEYBOARD,
    _FULL_FALLBACK_PREFIX,
    _RETRY_PREFIX,
    _UPLOAD_MAX_ATTEMPTS,
    _UPLOAD_RETRY_BASE_DELAY_SECONDS,
    _settings,
)

logger = logging.getLogger("telegram_share_bot.handlers")

_INVALID_CACHED_FILE_ID_MESSAGES = (
    "wrong file identifier/http url specified",
    "wrong remote file identifier specified",
    "file_id is invalid",
    "file identifier is invalid",
)


def is_invalid_cached_file_id_error(error: BaseException) -> bool:
    """Match only Telegram's specific responses for an unusable cached file ID."""
    if not isinstance(error, BadRequest):
        return False
    message = " ".join(str(error).casefold().split())
    return any(marker in message for marker in _INVALID_CACHED_FILE_ID_MESSAGES)


def _cancel_keyboard(result_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    strings.INLINE_CANCEL_BUTTON,
                    callback_data=f"{_CALLBACK_PREFIX}{result_id}",
                    style=KeyboardButtonStyle.DANGER,
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
            style=KeyboardButtonStyle.PRIMARY,
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
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Could not edit inline status category=bad_request")
    except TelegramError:
        logger.warning("Could not edit inline status category=telegram_error")


def _input_media(
    file_id: str,
    title: str,
    kind: MediaKind,
    *,
    caption: _RenderedCaption,
) -> InputMediaAnimation | InputMediaVideo | InputMediaAudio | InputMediaDocument:
    # Text stays literal; only the media title gets a link entity.
    if kind is MediaKind.ANIMATION:
        return InputMediaAnimation(
            media=file_id, caption=caption.text, caption_entities=caption.entities or None
        )
    if kind is MediaKind.VIDEO:
        return InputMediaVideo(
            media=file_id, caption=caption.text, caption_entities=caption.entities or None
        )
    if kind is MediaKind.AUDIO:
        return InputMediaAudio(
            media=file_id,
            caption=caption.text,
            caption_entities=caption.entities or None,
            title=title,
        )
    return InputMediaDocument(
        media=file_id, caption=caption.text, caption_entities=caption.entities or None
    )


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
        if stream.kind is MediaKind.ANIMATION:
            sent_msg = await context.bot.send_animation(
                chat_id=settings.storage_chat_id,
                animation=stream.direct_url,
                caption=caption,
                duration=stream.duration,
                disable_notification=True,
            )
        elif stream.kind is MediaKind.VIDEO:
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
    except TelegramError:
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
        caption = _RenderedCaption(_storage_upload_caption(effective_settings, media.title))
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
                if media.kind is MediaKind.ANIMATION:
                    return await context.bot.send_animation(
                        chat_id=chat_id,
                        animation=upload,
                        caption=caption.text,
                        caption_entities=caption.entities or None,
                        duration=media.duration,
                        disable_notification=True,
                        read_timeout=timeout,
                        write_timeout=timeout,
                    )
                if media.kind is MediaKind.VIDEO:
                    return await context.bot.send_video(
                        chat_id=chat_id,
                        video=upload,
                        caption=caption.text,
                        caption_entities=caption.entities or None,
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
                        caption=caption.text,
                        caption_entities=caption.entities or None,
                        duration=media.duration,
                        title=media.title,
                        disable_notification=True,
                        read_timeout=timeout,
                        write_timeout=timeout,
                    )
                return await context.bot.send_document(
                    chat_id=chat_id,
                    document=upload,
                    caption=caption.text,
                    caption_entities=caption.entities or None,
                    disable_notification=True,
                    read_timeout=timeout,
                    write_timeout=timeout,
                )
        except NetworkError as exc:
            last_error = exc
            if attempt >= _UPLOAD_MAX_ATTEMPTS:
                break
            delay = _UPLOAD_RETRY_BASE_DELAY_SECONDS * attempt
            logger.info(
                "Telegram upload retry attempt=%s/%s delay_s=%.1f category=network",
                attempt,
                _UPLOAD_MAX_ATTEMPTS,
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
    if cached.kind is MediaKind.ANIMATION:
        return await context.bot.send_animation(
            chat_id=chat_id,
            animation=cached.file_id,
            caption=caption.text,
            caption_entities=caption.entities or None,
            duration=cached.duration,
            disable_notification=True,
        )
    if cached.kind is MediaKind.VIDEO:
        return await context.bot.send_video(
            chat_id=chat_id,
            video=cached.file_id,
            caption=caption.text,
            caption_entities=caption.entities or None,
            duration=cached.duration,
            supports_streaming=True,
            disable_notification=True,
        )
    if cached.kind is MediaKind.AUDIO:
        return await context.bot.send_audio(
            chat_id=chat_id,
            audio=cached.file_id,
            caption=caption.text,
            caption_entities=caption.entities or None,
            duration=cached.duration,
            title=cached.title,
            disable_notification=True,
        )
    return await context.bot.send_document(
        chat_id=chat_id,
        document=cached.file_id,
        caption=caption.text,
        caption_entities=caption.entities or None,
        disable_notification=True,
    )


def _file_id_and_kind_from_message(message: Message) -> tuple[str, MediaKind]:
    if message.animation is not None:
        return message.animation.file_id, MediaKind.ANIMATION
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
    if height is None:
        animation_height = getattr(message.animation, "height", None)
        height = animation_height
    return height if isinstance(height, int) and height > 0 else None
