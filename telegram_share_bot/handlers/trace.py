"""Structured send-operation tracing helpers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from telegram_share_bot import strings
from telegram_share_bot.media.models import (
    DownloadError,
    MediaFormat,
    TimeRange,
    VideoUnavailableError,
)

logger = logging.getLogger("telegram_share_bot.handlers")


@dataclass(slots=True)
class _SendTrace:
    operation_id: str
    route: str
    media_format: MediaFormat
    time_range: TimeRange | None
    user_id: int | None
    started_at: float = field(default_factory=time.monotonic)
    cache_outcome: str = "miss"

    def event(
        self,
        stage: str,
        *,
        stage_started_at: float | None = None,
        failure: str = "none",
        level: int = logging.INFO,
        exc_info: bool = False,
    ) -> None:
        """Log an operation stage without its URL, caption, or Telegram token."""
        stage_seconds = (
            f"{time.monotonic() - stage_started_at:.1f}"
            if stage_started_at is not None
            else "-"
        )
        logger.log(
            level,
            "send op=%s user_id=%s route=%s format=%s scope=%s stage=%s elapsed_s=%.1f "
            "stage_s=%s cache=%s failure=%s",
            self.operation_id,
            self.user_id if self.user_id is not None else "-",
            self.route,
            self.media_format.value,
            "clip" if self.time_range is not None else "full",
            stage,
            time.monotonic() - self.started_at,
            stage_seconds,
            self.cache_outcome,
            failure,
            exc_info=exc_info,
        )


def _download_failure_category(exc: DownloadError) -> str:
    """Classify expected failures without putting source text in logs."""
    message = str(exc)
    if isinstance(exc, VideoUnavailableError):
        return "no_video"
    if message == strings.AUDIO_UNAVAILABLE:
        return "no_audio"
    if message.startswith("Download timed out"):
        return "timeout"
    if message.startswith(("Media exceeds", "Clips longer")):
        return "duration_limit"
    if message.startswith(("File exceeds", "File is too large", "The best source quality")):
        return "size_limit"
    return "source_retryable" if exc.retryable else "source"
