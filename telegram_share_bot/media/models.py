"""Core media request and result types shared across media operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from telegram_share_bot import strings


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class MediaFormat(str, Enum):
    """The requested output form, independent of the resulting file type."""

    VIDEO = "video"
    AUDIO = "audio"


class VideoQualityPolicy(str, Enum):
    """Per-send video selection policy; audio requests ignore this value."""

    AUTO = "auto-best"
    BEST = "source-best"
    BALANCED = "balanced"


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    title: str
    kind: MediaKind
    duration: int | None


@dataclass(frozen=True, slots=True)
class DirectMediaStream:
    direct_url: str
    title: str
    kind: MediaKind
    duration: int | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Clip window in whole seconds.

    ``end is None`` means through the end of the video (resolved at download).
    When ``end`` is set, it must be greater than ``start``.
    """

    start: int
    end: int | None = None

    @property
    def duration_seconds(self) -> int | None:
        if self.end is None:
            return None
        return self.end - self.start

    def cache_suffix(self) -> str:
        if self.end is None:
            return f"#t={self.start}-end"
        return f"#t={self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """Parsed inline/direct query: URL, optional caption, optional YouTube clip."""

    url: str | None
    custom_caption: str | None = None
    time_range: TimeRange | None = None


def _is_transient_download_error(message: str) -> bool:
    """Recognize network failures that may succeed when the user retries."""
    message = message.lower()
    markers = (
        "timed out",
        "timeout",
        "temporary failure",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "connection aborted",
        "remote end closed",
        "network is unreachable",
        "http error 408",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
    )
    return any(marker in message for marker in markers)


class DownloadError(Exception):
    """Raised when a URL cannot be downloaded within bot limits."""

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.retryable = (
            _is_transient_download_error(message) if retryable is None else retryable
        )


class VideoUnavailableError(DownloadError):
    """Raised when the requested link contains audio but no playable video."""

    def __init__(self) -> None:
        super().__init__(strings.VIDEO_UNAVAILABLE, retryable=False)
