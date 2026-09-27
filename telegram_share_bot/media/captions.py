"""Caption cleanup and selection for media results."""

from __future__ import annotations

from telegram_share_bot.config import TELEGRAM_CAPTION_MAX_LENGTH, CaptionMode


def sanitize_caption(text: str, *, max_length: int = 1024) -> str | None:
    """Strip control chars (except newline/tab) and enforce Telegram length."""
    cleaned = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def resolve_caption(
    mode: CaptionMode,
    *,
    media_title: str,
    custom_caption: str | None,
    max_length: int = TELEGRAM_CAPTION_MAX_LENGTH,
) -> str | None:
    """Pick the user-facing caption for the configured mode.

    Custom captions are never taken from cached media titles. Empty results
    become ``None`` (no caption). Callers must not enable ParseMode on captions.
    """
    if mode is CaptionMode.OFF:
        return None
    if mode is CaptionMode.CUSTOM:
        if custom_caption is None:
            return None
        return sanitize_caption(custom_caption, max_length=max_length)
    return sanitize_caption(media_title, max_length=max_length)
