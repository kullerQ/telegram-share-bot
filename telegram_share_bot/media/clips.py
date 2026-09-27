"""Clip range resolution and YouTube HLS clip downloading."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from yt_dlp.utils import download_range_func

from telegram_share_bot import strings
from telegram_share_bot.media.files import _cleanup_dir
from telegram_share_bot.media.formats import (
    _MAX_FORMAT_ATTEMPTS,
    _SOURCE_SIZE_MULTIPLIER,
    _format_bytes,
    _video_quality_label,
)
from telegram_share_bot.media.models import (
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.requests import MAX_CLIP_SECONDS
from telegram_share_bot.media.security import is_safe_media_url
from telegram_share_bot.media.transcode import (
    _convert_audio_for_telegram,
    _optimize_video_file,
    _run_bounded_clip_ffmpeg,
)

logger = logging.getLogger(__name__)





def _clamp_time_range(time_range: TimeRange, info: dict[str, Any]) -> TimeRange:
    """Resolve open-ended end; clamp to video duration; reject out-of-bounds."""
    duration_raw = info.get("duration")
    if not isinstance(duration_raw, (int, float)) or duration_raw <= 0:
        if time_range.end is None:
            raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
        return time_range

    duration = int(duration_raw)
    if time_range.start >= duration:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    end = duration if time_range.end is None else min(time_range.end, duration)
    if end <= time_range.start:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    return TimeRange(start=time_range.start, end=end)


def _ensure_clip_within_max(time_range: TimeRange) -> None:
    """Raise if a closed range exceeds the clip length cap."""
    duration = time_range.duration_seconds
    if duration is not None and duration > MAX_CLIP_SECONDS:
        raise DownloadError(
            strings.DOWNLOAD_CLIP_TOO_LONG.format(max_minutes=MAX_CLIP_SECONDS // 60)
        )


def _resolve_clip_range(time_range: TimeRange, info: dict[str, Any]) -> TimeRange:
    """Clamp to video length, enforce max clip, return a closed range."""
    resolved = _clamp_time_range(time_range, info)
    _ensure_clip_within_max(resolved)
    if resolved.end is None:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    return resolved


def _download_ranges_param(time_range: TimeRange) -> Any:
    """Build yt-dlp download_ranges callback for a closed TimeRange."""
    end = time_range.end
    if end is None:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    return cast(
        Any,
        download_range_func([], [(time_range.start, end)]),
    )


def _youtube_hls_clip_streams(
    info: dict[str, Any],
    clip_duration: int,
    max_file_bytes: int,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Find HLS audio and bounded H.264 video streams for a YouTube clip."""
    formats = info.get("formats")
    if not isinstance(formats, list):
        return None
    usable = [item for item in formats if isinstance(item, dict)]
    hls_protocols = {"m3u8", "m3u8_native"}
    audio = next(
        (
            item
            for item in reversed(usable)
            if item.get("protocol") in hls_protocols
            and item.get("vcodec") == "none"
            and item.get("resolution") == "audio only"
            and isinstance(item.get("url"), str)
        ),
        None,
    )
    if audio is None:
        return None

    source_duration = info.get("duration")
    duration_scale = (
        min(1.0, clip_duration / float(source_duration))
        if isinstance(source_duration, (int, float)) and source_duration > 0
        else 1.0
    )
    audio_allowance = max(1, int(192_000 * clip_duration / 8))
    ranked: list[tuple[tuple[float, ...], dict[str, Any]]] = []
    for item in usable:
        codec = str(item.get("vcodec") or "").lower()
        if (
            item.get("protocol") not in hls_protocols
            or not codec.startswith(("avc1", "h264"))
            or not isinstance(item.get("url"), str)
        ):
            continue
        estimated = _format_bytes(item, duration_scale, float(clip_duration))
        if (
            estimated is None
            or estimated + audio_allowance > max_file_bytes * _SOURCE_SIZE_MULTIPLIER
        ):
            continue
        height = item.get("height")
        fps = item.get("fps")
        bitrate = item.get("tbr")
        height_value = float(height) if isinstance(height, (int, float)) else 0.0
        fps_value = float(fps) if isinstance(fps, (int, float)) else 0.0
        bitrate_value = float(bitrate) if isinstance(bitrate, (int, float)) else 0.0
        if quality_policy is VideoQualityPolicy.BALANCED:
            rank: tuple[float, ...]
            rank = (
                0 if height_value == 720 and abs(fps_value - 60) <= 1 else 1,
                -height_value,
                -fps_value,
                -bitrate_value,
            )
        else:
            rank = (-height_value, -fps_value, -bitrate_value)
        ranked.append((rank, item))
    ranked.sort(key=lambda candidate: candidate[0])
    if quality_policy is VideoQualityPolicy.BALANCED and (
        not ranked
        or not (
            float(ranked[0][1].get("height") or 0) == 720
            and abs(float(ranked[0][1].get("fps") or 0) - 60) <= 1
        )
    ):
        return None
    limit = 1 if quality_policy is VideoQualityPolicy.BEST else _MAX_FORMAT_ATTEMPTS
    return audio, [item for _, item in ranked[:limit]]


def _download_hls_clip_stream(
    format_info: dict[str, Any],
    *,
    ffmpeg_bin: str,
    time_range: TimeRange,
    media_type: str,
    output: Path,
    deadline: float,
    abort_event: threading.Event | None,
    source_limit: int,
    timeout_seconds: int,
    expected_bytes: int | None = None,
    speed_limit_seconds: int = 0,
) -> None:
    stream_url = format_info.get("url")
    if not isinstance(stream_url, str) or not is_safe_media_url(stream_url, https_only=True):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)
    headers = format_info.get("http_headers")
    argv = [ffmpeg_bin, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if isinstance(headers, dict):
        safe_headers = [
            f"{key}: {value}\r\n"
            for key, value in headers.items()
            if isinstance(key, str)
            and isinstance(value, str)
            and "\r" not in key + value
            and "\n" not in key + value
        ]
        if safe_headers:
            argv.extend(["-headers", "".join(safe_headers)])
    argv.extend(
        [
            "-ss",
            str(time_range.start),
            "-i",
            stream_url,
            "-t",
            str(time_range.duration_seconds),
            "-map",
            f"0:{'a' if media_type == 'audio' else 'v'}:0",
            "-c",
            "copy",
        ]
    )
    if media_type == "audio":
        argv.extend(["-f", "ipod"])
    else:
        argv.extend(["-movflags", "+faststart"])
    argv.append(str(output))
    _run_bounded_clip_ffmpeg(
        argv,
        output,
        deadline=deadline,
        abort_event=abort_event,
        source_limit=source_limit,
        timeout_seconds=timeout_seconds,
        expected_bytes=expected_bytes,
        speed_limit_seconds=speed_limit_seconds,
        format_id=str(format_info.get("format_id") or "unknown"),
    )


def _download_youtube_hls_clip(
    info: dict[str, Any],
    *,
    work_dir: Path,
    time_range: TimeRange,
    media_format: MediaFormat,
    max_file_bytes: int,
    deadline: float,
    timeout_seconds: int,
    abort_event: threading.Event | None,
    on_optimizing: Callable[[], None] | None,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    max_estimated_download_seconds: int = 45,
) -> DownloadedMedia | None:
    clip_duration = time_range.duration_seconds
    if clip_duration is None:
        return None
    streams = _youtube_hls_clip_streams(info, clip_duration, max_file_bytes, quality_policy)
    if streams is None:
        return None
    audio, videos = streams
    if media_format is MediaFormat.VIDEO and not videos:
        return None
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_CLIP_FFMPEG_MISSING)
    source_limit = max_file_bytes * _SOURCE_SIZE_MULTIPLIER
    hls_dir = work_dir / "hls_clip"
    hls_dir.mkdir(parents=True, exist_ok=True)
    title = str(info.get("title") or "Media")[:64]
    started_at = time.monotonic()
    try:
        video_path = hls_dir / "video.mp4"
        if media_format is MediaFormat.VIDEO:
            last_error: DownloadError | None = None
            source_duration = info.get("duration")
            duration_scale = (
                min(1.0, clip_duration / float(source_duration))
                if isinstance(source_duration, (int, float)) and source_duration > 0
                else 1.0
            )
            for index, item in enumerate(videos):
                video_path.unlink(missing_ok=True)
                format_id = str(item.get("format_id") or "unknown")
                logger.info(
                    "Clip HLS video transfer started: format=%s quality=%s",
                    format_id,
                    _video_quality_label(item),
                )
                try:
                    stage_deadline = deadline
                    can_step_down = (
                        quality_policy is VideoQualityPolicy.AUTO
                        and max_estimated_download_seconds > 0
                        and index < len(videos) - 1
                    )
                    if can_step_down:
                        stage_deadline = min(
                            deadline,
                            time.monotonic() + max_estimated_download_seconds,
                        )
                    stage_timeout = max(1, int(stage_deadline - time.monotonic()))
                    _download_hls_clip_stream(
                        item,
                        ffmpeg_bin=ffmpeg_bin,
                        time_range=time_range,
                        media_type="video",
                        output=video_path,
                        deadline=stage_deadline,
                        abort_event=abort_event,
                        source_limit=source_limit,
                        timeout_seconds=stage_timeout,
                        expected_bytes=(
                            _format_bytes(item, duration_scale, float(clip_duration))
                            if can_step_down
                            else None
                        ),
                        speed_limit_seconds=(
                            max_estimated_download_seconds if can_step_down else 0
                        ),
                    )
                    logger.info(
                        "Clip HLS video ready: format=%s bytes=%d",
                        format_id,
                        video_path.stat().st_size,
                    )
                    break
                except DownloadError as exc:
                    last_error = exc
                    logger.info("Clip HLS video format %s failed: %s", format_id, exc)
                    if abort_event is not None and abort_event.is_set():
                        raise
            else:
                raise last_error or DownloadError(strings.DOWNLOAD_FAILED_GENERIC)

        audio_path = hls_dir / "audio.m4a"
        audio_id = str(audio.get("format_id") or "unknown")
        logger.info("Clip HLS audio transfer started: format=%s", audio_id)
        _download_hls_clip_stream(
            audio,
            ffmpeg_bin=ffmpeg_bin,
            time_range=time_range,
            media_type="audio",
            output=audio_path,
            deadline=deadline,
            abort_event=abort_event,
            source_limit=source_limit,
            timeout_seconds=timeout_seconds,
        )
        logger.info(
            "Clip HLS audio ready: format=%s bytes=%d",
            audio_id,
            audio_path.stat().st_size,
        )
        if media_format is MediaFormat.AUDIO:
            selected = work_dir / "selected.m4a"
            if audio_path.stat().st_size > max_file_bytes:
                audio_path = _convert_audio_for_telegram(
                    audio_path,
                    max_file_bytes=max_file_bytes,
                    deadline=deadline,
                    abort_event=abort_event,
                )
            audio_path.replace(selected)
            return DownloadedMedia(selected, title, MediaKind.AUDIO, clip_duration)

        if video_path.stat().st_size + audio_path.stat().st_size > source_limit:
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=source_limit // (1024 * 1024))
            )
        merged = hls_dir / "merged.mp4"
        _run_bounded_clip_ffmpeg(
            [
                ffmpeg_bin,
                "-nostdin",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(merged),
            ],
            merged,
            deadline=deadline,
            abort_event=abort_event,
            source_limit=source_limit,
            timeout_seconds=timeout_seconds,
        )
        merged = _optimize_video_file(
            merged,
            max_file_bytes=max_file_bytes,
            deadline=deadline,
            abort_event=abort_event,
            on_optimizing=on_optimizing,
        )
        selected = work_dir / "selected.mp4"
        merged.replace(selected)
        logger.info(
            "Clip HLS video and audio merged in %.1fs: bytes=%d",
            time.monotonic() - started_at,
            selected.stat().st_size,
        )
        return DownloadedMedia(selected, title, MediaKind.VIDEO, clip_duration)
    finally:
        _cleanup_dir(hls_dir)
