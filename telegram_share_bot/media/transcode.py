"""Helpers for transcoding and validating media for Telegram."""

from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from telegram_share_bot import strings
from telegram_share_bot.media.formats import (
    _QUALITY_SAMPLE_SECONDS,
    _SOURCE_SIZE_MULTIPLIER,
    _projected_transfer_seconds,
)
from telegram_share_bot.media.models import DownloadError, VideoUnavailableError

logger = logging.getLogger(__name__)


def _run_bounded_clip_ffmpeg(
    argv: list[str],
    output: Path,
    *,
    deadline: float,
    abort_event: threading.Event | None,
    source_limit: int,
    timeout_seconds: int,
    expected_bytes: int | None = None,
    speed_limit_seconds: int = 0,
    format_id: str = "unknown",
) -> None:
    """Stop the actual ffmpeg process when a clip stage times out or is cancelled."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc
    started_at = time.monotonic()
    next_speed_sample = _QUALITY_SAMPLE_SECONDS
    try:
        while True:
            if abort_event is not None and abort_event.is_set():
                raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError(
                    strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                )
            output_size = output.stat().st_size if output.exists() else 0
            if output_size > source_limit:
                raise DownloadError(
                    strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=source_limit // (1024 * 1024))
                )
            elapsed = time.monotonic() - started_at
            if (
                speed_limit_seconds > 0
                and expected_bytes is not None
                and elapsed >= next_speed_sample
                and output_size > 0
            ):
                projected = _projected_transfer_seconds(elapsed, output_size, expected_bytes)
                if projected is not None:
                    logger.info(
                        "Clip video transfer estimate: format=%s projected=%.1fs target=%ds",
                        format_id,
                        projected,
                        speed_limit_seconds,
                    )
                    if projected > speed_limit_seconds:
                        raise DownloadError(strings.DOWNLOAD_SLOW_SOURCE, retryable=True)
                next_speed_sample = elapsed + _QUALITY_SAMPLE_SECONDS
            try:
                return_code = process.wait(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if return_code != 0 or not output.exists() or output.stat().st_size <= 0:
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC, retryable=True)
        if output.stat().st_size > source_limit:
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=source_limit // (1024 * 1024))
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _convert_audio_for_telegram(
    path: Path,
    *,
    max_file_bytes: int,
    deadline: float,
    abort_event: threading.Event | None,
) -> Path:
    """Return MP3/M4A audio, transcoding incompatible or oversized sources."""
    if path.suffix.lower() in {".m4a", ".mp3"} and path.stat().st_size <= max_file_bytes:
        return path
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_AUDIO_FFMPEG_MISSING)
    for index, bitrate in enumerate(("128k", "96k"), start=1):
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0))
        output = path.parent / f"telegram_audio_{index}.m4a"
        output.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                [
                    ffmpeg_bin,
                    "-nostdin",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path),
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    bitrate,
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                check=False,
                capture_output=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0)) from exc
        except OSError as exc:
            raise DownloadError(strings.DOWNLOAD_AUDIO_CONVERT_FAILED) from exc
        if abort_event is not None and abort_event.is_set():
            output.unlink(missing_ok=True)
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        if (
            completed.returncode == 0
            and output.exists()
            and 0 < output.stat().st_size <= max_file_bytes
        ):
            if output != path:
                path.unlink(missing_ok=True)
            return output
        output.unlink(missing_ok=True)
    raise DownloadError(
        strings.DOWNLOAD_TOO_LARGE.format(
            size_mb=path.stat().st_size // (1024 * 1024),
            max_mb=max_file_bytes // (1024 * 1024),
        )
    )


@dataclass(frozen=True, slots=True)
class _VideoProbe:
    duration: float
    video_codec: str
    pixel_format: str
    audio_codec: str | None
    width: int
    height: int

    @property
    def compatible(self) -> bool:
        return (
            self.video_codec == "h264"
            and self.pixel_format == "yuv420p"
            and self.audio_codec in (None, "aac")
        )


def _probe_video_file(path: Path, deadline: float) -> _VideoProbe:
    """Verify actual streams; an MP4 extension alone does not imply video."""
    ffmpeg_bin = shutil.which("ffmpeg")
    remaining = deadline - time.monotonic()
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_MISSING)
    if remaining <= 0:
        raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0))
    try:
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-i",
                str(path),
                "-t",
                "0",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            timeout=min(10.0, remaining),
        )
        if result.returncode != 0:
            raise ValueError("ffmpeg rejected the output")
        duration = 0.0
        video_codec = ""
        pixel_format = ""
        audio_codec: str | None = None
        video_stream_seen = False
        width = height = 0
        for line in (result.stderr or b"").decode("utf-8", errors="replace").splitlines():
            # Only inspect the input: FFmpeg also prints transcoded output streams.
            if line.startswith(("Stream mapping:", "Output #")):
                break
            duration_match = re.match(
                r"^\s*Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)", line
            )
            if duration_match is not None:
                hours, minutes, seconds = duration_match.groups()
                duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            stream_match = re.match(
                r"^\s*Stream #0:\d+(?:\[[^]]+\])?(?:\([^)]*\))?: "
                r"(Video|Audio): ([A-Za-z0-9_]+)\b(.*)$",
                line,
            )
            if stream_match is None:
                continue
            kind, codec, details = stream_match.groups()
            if kind == "Audio" and audio_codec is None:
                audio_codec = codec
            if kind != "Video" or "(attached pic)" in details:
                continue
            video_stream_seen = True
            if video_codec:
                continue
            dimensions = re.search(r"(?<!\w)(\d{2,5})x(\d{2,5})(?!\d)", details)
            if dimensions is None:
                continue
            video_codec = codec
            width, height = map(int, dimensions.groups())
            pixel_match = re.search(
                r",\s*([A-Za-z0-9_]+)(?:\([^)]*\))?,\s*\d{2,5}x\d{2,5}\b",
                details,
            )
            pixel_format = pixel_match.group(1) if pixel_match is not None else ""
        if not video_stream_seen and audio_codec is not None:
            raise VideoUnavailableError()
        if width <= 0 or height <= 0 or not math.isfinite(duration):
            raise ValueError("invalid video dimensions or duration")
        return _VideoProbe(
            duration,
            video_codec,
            pixel_format,
            audio_codec,
            width,
            height,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        TypeError,
    ) as exc:
        logger.warning("Downloaded output is not a complete video: %s", path.name)
        raise DownloadError(strings.DOWNLOAD_FAILED_INCOMPLETE) from exc


def _optimize_video_file(
    path: Path,
    *,
    max_file_bytes: int,
    deadline: float,
    abort_event: threading.Event | None,
    on_optimizing: Callable[[], None] | None,
) -> Path:
    """Verify video and fit H.264/AAC output to the budget in at most two encodes."""
    probe = _probe_video_file(path, deadline)
    source_size = path.stat().st_size
    if source_size <= max_file_bytes and probe.compatible and path.suffix.lower() == ".mp4":
        logger.info(
            "Video verified for Telegram: bytes=%d quality=%dx%d codec=%s",
            source_size,
            probe.width,
            probe.height,
            probe.video_codec,
        )
        return path
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_MISSING)
    if probe.duration <= 0:
        raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_FAILED)
    audio_rate = 192_000 if probe.audio_codec is not None else 0
    video_rate = int(max_file_bytes * 8 * 0.94 / probe.duration) - audio_rate
    if video_rate <= 0:
        raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_FAILED)
    logger.info(
        "Optimizing video for Telegram: source_bytes=%d limit_bytes=%d "
        "quality=%dx%d source_codec=%s target_video_bps=%d",
        source_size,
        max_file_bytes,
        probe.width,
        probe.height,
        probe.video_codec,
        video_rate,
    )
    if on_optimizing is not None:
        on_optimizing()

    for index in range(1, 3):
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0))
        output = path.parent / f"optimized_{index}.mp4"
        output.unlink(missing_ok=True)
        copy_video = (
            index == 1
            and source_size <= max_file_bytes
            and probe.video_codec == "h264"
            and probe.pixel_format == "yuv420p"
        )
        video_options = (
            ["-c:v", "copy"]
            if copy_video
            else [
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv,format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                str(video_rate),
                "-pix_fmt",
                "yuv420p",
            ]
        )
        argv = [
            ffmpeg_bin,
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            *video_options,
            "-c:a",
            "copy" if copy_video and probe.audio_codec in (None, "aac") else "aac",
            "-b:a",
            str(audio_rate or 192_000),
            "-movflags",
            "+faststart",
            str(output),
        ]
        _run_bounded_clip_ffmpeg(
            argv,
            output,
            deadline=deadline,
            abort_event=abort_event,
            source_limit=max_file_bytes * _SOURCE_SIZE_MULTIPLIER,
            timeout_seconds=max(1, int(remaining)),
        )
        output_probe = _probe_video_file(output, deadline)
        if not output_probe.compatible or (probe.audio_codec and not output_probe.audio_codec):
            raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_FAILED)
        output_size = output.stat().st_size
        logger.info(
            "Video optimization attempt %d: output_bytes=%d limit_bytes=%d quality=%dx%d",
            index,
            output_size,
            max_file_bytes,
            output_probe.width,
            output_probe.height,
        )
        if 0 < output_size <= max_file_bytes:
            path.unlink(missing_ok=True)
            return output
        # Retry from the original source, never from an already lossy encode.
        video_rate = max(1, int(video_rate * max_file_bytes / max(1, output_size) * 0.94))
        output.unlink(missing_ok=True)
    raise DownloadError(
        strings.DOWNLOAD_TOO_LARGE.format(
            size_mb=source_size // (1024 * 1024),
            max_mb=max_file_bytes // (1024 * 1024),
        )
    )
