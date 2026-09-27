"""Shared duration limits for full media sends."""

from __future__ import annotations

from telegram_share_bot import strings
from telegram_share_bot.media.models import DownloadError


def ensure_full_media_duration(duration: object, max_duration_seconds: int) -> None:
    """Reject known overlong full media before transfer or conversion."""
    if (
        max_duration_seconds <= 0
        or not isinstance(duration, (int, float))
        or duration <= max_duration_seconds
    ):
        return
    duration_limit = (
        f"{max_duration_seconds // 60} min"
        if max_duration_seconds % 60 == 0
        else f"{max_duration_seconds} sec"
    )
    raise DownloadError(strings.DOWNLOAD_MEDIA_TOO_LONG.format(duration_limit=duration_limit))
