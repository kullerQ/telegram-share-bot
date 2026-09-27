"""Caption and per-user sharing preference helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import KeyboardButtonStyle
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.config import CaptionMode, Settings
from telegram_share_bot.handlers.state import _settings
from telegram_share_bot.media.captions import sanitize_caption
from telegram_share_bot.media.models import MediaFormat, VideoQualityPolicy
from telegram_share_bot.storage.user_settings import (
    CaptionPreference,
    UserSettingsStore,
    UserSharingSettings,
)


@dataclass(frozen=True, slots=True)
class _RenderedCaption:
    text: str | None
    entities: tuple[MessageEntity, ...] = ()


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


def _selected_caption(
    preferences: UserSharingSettings, settings: Settings
) -> CaptionPreference | None:
    if preferences.caption is not None:
        return preferences.caption
    if settings.caption_mode is CaptionMode.MEDIA:
        return CaptionPreference.MEDIA_TITLE
    if settings.caption_mode is CaptionMode.CUSTOM:
        return CaptionPreference.CUSTOM
    return None


def _linked_title_caption(media_title: str, original_url: str) -> _RenderedCaption:
    title = sanitize_caption(media_title)
    if title is None:
        return _RenderedCaption(None)
    if not original_url:
        return _RenderedCaption(title)
    # Telegram entity offsets and lengths count UTF-16 code units.
    title_length = len(title.encode("utf-16-le")) // 2
    link = MessageEntity(MessageEntity.TEXT_LINK, 0, title_length, url=original_url)
    return _RenderedCaption(title, (link,))


def _resolve_user_caption(
    preferences: UserSharingSettings,
    settings: Settings,
    *,
    media_title: str,
    original_url: str,
    custom_caption: str | None,
) -> _RenderedCaption:
    if preferences.caption is None:
        if settings.caption_mode is CaptionMode.MEDIA:
            return _linked_title_caption(media_title, original_url)
        if settings.caption_mode is CaptionMode.CUSTOM:
            return _RenderedCaption(
                sanitize_caption(custom_caption) if custom_caption is not None else None
            )
        return _RenderedCaption(None)
    if custom_caption is not None:
        return _RenderedCaption(sanitize_caption(custom_caption))
    if preferences.caption is CaptionPreference.MEDIA_TITLE:
        return _linked_title_caption(media_title, original_url)
    return _RenderedCaption(None)


def _settings_text(preferences: UserSharingSettings, settings: Settings) -> str:
    def option(selected: bool, label: str, description: str) -> str:
        marker = "✅ " if selected else "• "
        styled = f"<b>{label}</b>" if selected else label
        return f"{marker}{styled} — {description}"

    caption = _selected_caption(preferences, settings)
    return strings.SETTINGS_MESSAGE.format(
        quality_auto=option(
            preferences.video_quality is VideoQualityPolicy.AUTO,
            "Auto",
            "adapts to the link and download speed",
        ),
        quality_best=option(
            preferences.video_quality is VideoQualityPolicy.BEST,
            "Best",
            "tries the highest quality available",
        ),
        quality_balanced=option(
            preferences.video_quality is VideoQualityPolicy.BALANCED,
            "Balanced",
            "good picture with a smaller download",
        ),
        caption_custom=option(
            caption is CaptionPreference.CUSTOM,
            "Custom",
            "uses text you add after a link",
        ),
        caption_title=option(
            caption is CaptionPreference.MEDIA_TITLE,
            "Media title",
            "a clickable title linked to the original video",
        ),
        caption_note=(
            "\n<i>Captions are off until you choose an option.</i>" if caption is None else ""
        ),
        format_unspecified=option(
            preferences.default_format is None,
            "Not specified",
            "choose Video or Audio each time",
        ),
        format_video=option(
            preferences.default_format is MediaFormat.VIDEO,
            "Video",
            "send video automatically",
        ),
        format_audio=option(
            preferences.default_format is MediaFormat.AUDIO,
            "Audio",
            "send audio automatically",
        ),
    )


def _settings_keyboard(
    user_id: int, preferences: UserSharingSettings, settings: Settings
) -> InlineKeyboardMarkup:
    def choice(field: str, value: str, label: str, selected: bool) -> InlineKeyboardButton:
        marker = "✅ " if selected else ""
        return InlineKeyboardButton(
            f"{marker}{label}", callback_data=f"settings:{user_id}:{field}:{value}"
        )

    caption = _selected_caption(preferences, settings)
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
                    CaptionPreference.CUSTOM.value,
                    "Custom",
                    caption is CaptionPreference.CUSTOM,
                ),
                choice(
                    "caption",
                    CaptionPreference.MEDIA_TITLE.value,
                    "Media title",
                    caption is CaptionPreference.MEDIA_TITLE,
                ),
            ],
            [
                choice(
                    "format", "unspecified", "Not specified", preferences.default_format is None
                ),
                choice("format", "video", "Video", preferences.default_format is MediaFormat.VIDEO),
                choice("format", "audio", "Audio", preferences.default_format is MediaFormat.AUDIO),
            ],
            [
                InlineKeyboardButton(
                    "Reset to default",
                    callback_data=f"settings:{user_id}:all:reset",
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
        ]
    )
