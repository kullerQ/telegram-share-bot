"""Track source size and transfer speed across format attempts."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.media.formats import _QUALITY_SAMPLE_SECONDS, _projected_transfer_seconds


@dataclass(slots=True)
class TransferMonitor:
    """Enforce one source bound and sample a video's projected transfer time."""

    source_limit: int
    timeout_seconds: int
    is_clip: bool
    abort_event: threading.Event | None
    max_estimated_download_seconds: int
    monotonic: Callable[[], float]
    logger: logging.Logger
    source_bytes: dict[str, int] = field(default_factory=dict)
    source_too_large: threading.Event = field(default_factory=threading.Event)
    slow_source: threading.Event = field(default_factory=threading.Event)
    transfer_started_at: float = 0.0
    video_estimated_bytes: int | None = None
    can_step_down: bool = False
    next_quality_sample: float = _QUALITY_SAMPLE_SECONDS

    def start_attempt(self, *, can_step_down: bool) -> None:
        self.source_bytes.clear()
        self.source_too_large.clear()
        self.slow_source.clear()
        self.transfer_started_at = self.monotonic()
        self.next_quality_sample = _QUALITY_SAMPLE_SECONDS
        self.can_step_down = can_step_down
        self.video_estimated_bytes = None

    def hook(self, data: dict[str, Any]) -> None:
        """Reject oversized or slow source transfers from yt-dlp progress events."""
        if self.abort_event is not None and self.abort_event.is_set():
            raise yt_dlp.utils.DownloadError(
                strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=self.timeout_seconds)
            )
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        if isinstance(downloaded, (int, float)):
            key = str(
                data.get("filename")
                or data.get("tmpfilename")
                or data.get("format_id")
                or "source"
            )
            self.source_bytes[key] = int(downloaded)
        if isinstance(total, (int, float)) and total > 0 and not self.is_clip:
            key = str(
                data.get("filename")
                or data.get("tmpfilename")
                or data.get("format_id")
                or "source"
            )
            self.source_bytes[key] = max(self.source_bytes.get(key, 0), int(total))
        if sum(self.source_bytes.values()) > self.source_limit:
            self.source_too_large.set()
            raise yt_dlp.utils.DownloadError("Source size bound exceeded")
        if (
            not self.can_step_down
            or data.get("status") != "downloading"
            or not isinstance(downloaded, (int, float))
            or downloaded <= 0
        ):
            return
        stream_info = data.get("info_dict")
        if isinstance(stream_info, dict) and stream_info.get("vcodec") == "none":
            return
        elapsed = self.monotonic() - self.transfer_started_at
        if elapsed < self.next_quality_sample:
            return
        total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate")
        if not isinstance(total_bytes, (int, float)) or total_bytes <= 0:
            total_bytes = self.video_estimated_bytes
        if isinstance(total_bytes, (int, float)):
            projected = _projected_transfer_seconds(elapsed, int(downloaded), int(total_bytes))
            if projected is not None:
                self.logger.info(
                    "Video transfer estimate: projected=%.1fs target=%ds",
                    projected,
                    self.max_estimated_download_seconds,
                )
                if projected > self.max_estimated_download_seconds:
                    self.slow_source.set()
                    raise yt_dlp.utils.DownloadError("Video transfer exceeds time target")
        self.next_quality_sample = elapsed + _QUALITY_SAMPLE_SECONDS
