"""Download media URLs with yt-dlp under size and time limits."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yt_dlp
from yt_dlp.utils import download_range_func

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
)
from telegram_share_bot.media.formats import (
    _MAX_FORMAT_ATTEMPTS,
    _QUALITY_SAMPLE_SECONDS,
    _SOURCE_SIZE_MULTIPLIER,
    _audio_format_candidates,
    _default_audio_selector,
    _default_video_selector,
    _format_bytes,
    _format_candidates,
    _is_audio_unavailable_error,
    _projected_transfer_seconds,
    _set_attempt_format_selector,
    _set_attempt_output_template,
    _video_quality_for_selector,
    _video_quality_label,
)
from telegram_share_bot.media.metadata import (
    _evict_extract_info,
    _extract_info_cached,
    _looks_like_stale_cdn_url,
    _pick_info,
)
from telegram_share_bot.media.models import (
    DirectMediaStream,
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
    VideoUnavailableError,
    _is_transient_download_error,
)
from telegram_share_bot.media.requests import (
    MAX_CLIP_SECONDS,
    format_time_range,
)
from telegram_share_bot.media.security import (
    _safe_dns_resolution,
    is_allowed_media_host,
    is_https_url,
    is_safe_media_url,
)
from telegram_share_bot.platforms.urls import is_youtube_url, safe_url_for_log

logger = logging.getLogger(__name__)


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".temp", ".aria2"}


def _classify(path: Path) -> MediaKind:
    return _classify_ext(path.suffix)


def _classify_ext(ext: str) -> MediaKind:
    suffix = f".{ext.lower().lstrip('.')}"
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if suffix in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    return MediaKind.DOCUMENT


def _is_complete_file(path: Path) -> bool:
    name_lower = path.name.lower()
    return not any(name_lower.endswith(ext) for ext in INCOMPLETE_SUFFIXES)


def _resolve_downloaded_path(info: Any, work_dir: Path, ydl: yt_dlp.YoutubeDL) -> Path:
    # Prefer the postprocessor's final output over individual download components.
    final_path = info.get("filepath") if isinstance(info, dict) else None
    if isinstance(final_path, str):
        final = Path(final_path)
        if final.is_file() and _is_complete_file(final):
            return final
    prepared = Path(ydl.prepare_filename(info))
    if prepared.is_file() and _is_complete_file(prepared):
        return prepared
    requested = info.get("requested_downloads") if isinstance(info, dict) else None
    if isinstance(requested, list) and requested:
        first = requested[0]
        if isinstance(first, dict):
            filepath = first.get("filepath")
            if isinstance(filepath, str) and filepath:
                p = Path(filepath)
                if p.exists() and _is_complete_file(p):
                    return p

    completed_files = [p for p in work_dir.glob("*") if p.is_file() and _is_complete_file(p)]
    if not completed_files:
        partial_files = [p for p in work_dir.glob("*") if p.is_file() and not _is_complete_file(p)]
        if partial_files:
            raise DownloadError(strings.DOWNLOAD_FAILED_INCOMPLETE)
        raise DownloadError(strings.DOWNLOAD_NO_FILE)

    return max(completed_files, key=lambda p: p.stat().st_size)






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
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
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


def _download_sync(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
    abort_event: threading.Event | None = None,
    work_dir_holder: list[Path] | None = None,
    *,
    https_only: bool = False,
    allowed_hosts: frozenset[str] | None = None,
    slideshow_slide_ms: int = 2500,
    slideshow_max_images: int = 35,
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    max_estimated_download_seconds: int = DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    on_optimizing: Callable[[], None] | None = None,
) -> DownloadedMedia:
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    source_limit = max_file_bytes * _SOURCE_SIZE_MULTIPLIER
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    if time_range is not None:
        _ensure_clip_within_max(time_range)
        if shutil.which("ffmpeg") is None:
            raise DownloadError(strings.DOWNLOAD_CLIP_FFMPEG_MISSING)

    work_dir = download_dir / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    if work_dir_holder is not None:
        work_dir_holder.append(work_dir)

    # TikTok photo posts: yt-dlp has no slideshow formats — compile images + sound.
    # Clip ranges only apply to YouTube; skip slideshow for ranged downloads.
    if time_range is None:
        from telegram_share_bot.slideshow import download_tiktok_slideshow

        try:
            slideshow = download_tiktok_slideshow(
                url,
                work_dir,
                max_file_bytes=source_limit,
                timeout_seconds=max(1, int(deadline - time.monotonic())),
                slide_ms=slideshow_slide_ms,
                max_images=slideshow_max_images,
                images_loop=slideshow_images_loop,
                abort_event=abort_event,
                https_only=https_only,
                allowed_hosts=allowed_hosts,
                media_format=media_format,
                max_media_duration_seconds=max_media_duration_seconds,
            )
            if slideshow is not None:
                ensure_full_media_duration(slideshow.duration, max_media_duration_seconds)
                if media_format is MediaFormat.AUDIO:
                    result_path = _convert_audio_for_telegram(
                        slideshow.path,
                        max_file_bytes=max_file_bytes,
                        deadline=deadline,
                        abort_event=abort_event,
                    )
                    result_kind = MediaKind.AUDIO
                else:
                    result_path = _optimize_video_file(
                        slideshow.path,
                        max_file_bytes=max_file_bytes,
                        deadline=deadline,
                        abort_event=abort_event,
                        on_optimizing=on_optimizing,
                    )
                    result_kind = slideshow.kind
                return DownloadedMedia(
                    path=result_path,
                    title=slideshow.title,
                    kind=result_kind,
                    duration=slideshow.duration,
                )
        except DownloadError:
            _cleanup_dir(work_dir)
            raise
        except Exception as exc:
            _cleanup_dir(work_dir)
            logger.warning("Slideshow path failed for %s: %s", safe_url_for_log(url), exc)
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    outtmpl = str(work_dir / "%(title).80B [%(id)s].%(ext)s")
    is_clip = time_range is not None

    source_bytes: dict[str, int] = {}
    source_too_large = threading.Event()
    slow_source = threading.Event()
    transfer_started_at = 0.0
    video_estimated_bytes: int | None = None
    can_step_down = False
    next_quality_sample = _QUALITY_SAMPLE_SECONDS

    def _progress_hook(d: dict[str, Any]) -> None:
        nonlocal next_quality_sample
        if abort_event is not None and abort_event.is_set():
            raise yt_dlp.utils.DownloadError(
                strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
            )
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if isinstance(downloaded, (int, float)):
            key = str(d.get("filename") or d.get("tmpfilename") or d.get("format_id") or "source")
            source_bytes[key] = int(downloaded)
        if isinstance(total, (int, float)) and total > 0 and not is_clip:
            key = str(d.get("filename") or d.get("tmpfilename") or d.get("format_id") or "source")
            source_bytes[key] = max(source_bytes.get(key, 0), int(total))
        if sum(source_bytes.values()) > source_limit:
            source_too_large.set()
            raise yt_dlp.utils.DownloadError("Source size bound exceeded")
        if (
            not can_step_down
            or d.get("status") != "downloading"
            or not isinstance(downloaded, (int, float))
            or downloaded <= 0
        ):
            return
        stream_info = d.get("info_dict")
        if isinstance(stream_info, dict) and stream_info.get("vcodec") == "none":
            return
        elapsed = time.monotonic() - transfer_started_at
        if elapsed < next_quality_sample:
            return
        total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not isinstance(total_bytes, (int, float)) or total_bytes <= 0:
            total_bytes = video_estimated_bytes
        if isinstance(total_bytes, (int, float)):
            projected = _projected_transfer_seconds(elapsed, int(downloaded), int(total_bytes))
            if projected is not None:
                logger.info(
                    "Video transfer estimate: projected=%.1fs target=%ds",
                    projected,
                    max_estimated_download_seconds,
                )
                if projected > max_estimated_download_seconds:
                    slow_source.set()
                    raise yt_dlp.utils.DownloadError("Video transfer exceeds time target")
        next_quality_sample = elapsed + _QUALITY_SAMPLE_SECONDS

    # yt-dlp selectors are chosen from ranked metadata and retried at lower
    # quality when the measured output exceeds Telegram's limit.
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
        "retries": 2,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook],
        "format": _default_video_selector(time_range, source_limit),
    }

    if not is_clip:
        ydl_opts["max_filesize"] = source_limit

    effective_range = time_range

    try:
        with _safe_dns_resolution():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                    )

                # Probe metadata first so live streams abort before any media bytes land.
                # Reuse extract_info from get_direct_stream when still warm.
                metadata_started_at = time.monotonic()
                extracted, from_cache = _extract_info_cached(ydl, url)
                info = _pick_info(extracted)
                formats = info.get("formats")
                if media_format is MediaFormat.VIDEO and isinstance(formats, list) and formats:
                    if all(
                        isinstance(item, dict) and item.get("vcodec") == "none"
                        for item in formats
                    ) and any(
                        isinstance(item, dict) and item.get("acodec") not in (None, "none")
                        for item in formats
                    ):
                        raise VideoUnavailableError()
                if effective_range is None:
                    ensure_full_media_duration(info.get("duration"), max_media_duration_seconds)
                if is_clip:
                    logger.info(
                        "Clip metadata resolved in %.1fs (%s) for %s",
                        time.monotonic() - metadata_started_at,
                        "cache" if from_cache else "source",
                        safe_url_for_log(url),
                    )

                if effective_range is not None:
                    effective_range = _resolve_clip_range(effective_range, info)
                    ydl.params["download_ranges"] = _download_ranges_param(effective_range)

                if (
                    effective_range is not None
                    and is_youtube_url(url)
                    and quality_policy is not VideoQualityPolicy.BEST
                ):
                    hls_clip = _download_youtube_hls_clip(
                        info,
                        work_dir=work_dir,
                        time_range=effective_range,
                        media_format=media_format,
                        max_file_bytes=max_file_bytes,
                        deadline=deadline,
                        timeout_seconds=timeout_seconds,
                        abort_event=abort_event,
                        on_optimizing=on_optimizing,
                        quality_policy=quality_policy,
                        max_estimated_download_seconds=max_estimated_download_seconds,
                    )
                    if hls_clip is not None:
                        return hls_clip

                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                    )

                duration_scale = 1.0
                source_duration = info.get("duration")
                if (
                    effective_range is not None
                    and effective_range.duration_seconds is not None
                    and isinstance(source_duration, (int, float))
                    and source_duration > 0
                ):
                    duration_scale = min(
                        1.0, effective_range.duration_seconds / float(source_duration)
                    )
                candidates = (
                    _audio_format_candidates(
                        info,
                        max_file_bytes=max_file_bytes,
                        duration_scale=duration_scale,
                    )
                    if media_format is MediaFormat.AUDIO
                    else _format_candidates(
                        info,
                        max_file_bytes=max_file_bytes,
                        duration_scale=duration_scale,
                        quality_policy=quality_policy,
                    )
                )
                selectors = [candidate.selector for candidate in candidates]
                if media_format is MediaFormat.VIDEO and quality_policy is VideoQualityPolicy.BEST:
                    selectors = selectors[:1]
                fallback_selector = (
                    _default_audio_selector(effective_range, source_limit)
                    if media_format is MediaFormat.AUDIO
                    else _default_video_selector(effective_range, source_limit)
                )
                if (
                    media_format is MediaFormat.VIDEO
                    and quality_policy is VideoQualityPolicy.BEST
                    and not selectors
                ):
                    raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED)
                if not selectors or (
                    media_format is MediaFormat.AUDIO and fallback_selector not in selectors
                ):
                    selectors.append(fallback_selector)

                duration_raw = info.get("duration")
                duration = (
                    effective_range.duration_seconds
                    if effective_range is not None
                    else int(duration_raw)
                    if isinstance(duration_raw, (int, float))
                    else None
                )
                title = str(info.get("title") or "Media")[:64]
                best_oversized: tuple[Path, int] | None = None
                last_error: BaseException | None = None
                refreshed = False

                for attempt, selector in enumerate(selectors, start=1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                        )
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                        )
                    attempt_dir = work_dir / f"attempt_{attempt}"
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    _set_attempt_output_template(
                        ydl, str(attempt_dir / "%(title).80B [%(id)s].%(ext)s")
                    )
                    _set_attempt_format_selector(ydl, selector)
                    source_bytes.clear()
                    source_too_large.clear()
                    slow_source.clear()
                    transfer_started_at = time.monotonic()
                    next_quality_sample = _QUALITY_SAMPLE_SECONDS
                    can_step_down = (
                        media_format is MediaFormat.VIDEO
                        and quality_policy is VideoQualityPolicy.AUTO
                        and max_estimated_download_seconds > 0
                        and attempt < len(selectors)
                    )
                    estimate = next(
                        (
                            candidate.estimated_size
                            for candidate in candidates
                            if candidate.selector == selector
                        ),
                        None,
                    )
                    if estimate is not None and estimate > source_limit:
                        logger.info(
                            "Selected source exceeds download bound: "
                            "format=%s estimated_bytes=%d limit_bytes=%d",
                            selector,
                            estimate,
                            source_limit,
                        )
                        last_error = DownloadError(
                            strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                                max_mb=source_limit // (1024 * 1024)
                            )
                        )
                        continue
                    video_estimated_bytes = None
                    if media_format is MediaFormat.VIDEO:
                        video_format_id = selector.split("+", maxsplit=1)[0]
                        formats = info.get("formats")
                        if isinstance(formats, list):
                            for item in formats:
                                if (
                                    isinstance(item, dict)
                                    and item.get("format_id") == video_format_id
                                ):
                                    video_estimated_bytes = _format_bytes(
                                        item,
                                        duration_scale,
                                        (
                                            float(effective_range.duration_seconds)
                                            if effective_range is not None
                                            and effective_range.duration_seconds is not None
                                            else None
                                        ),
                                    )
                                    break
                        logger.info(
                            "Video format attempt started: "
                            "format=%s quality=%s estimated_bytes=%s range=%s",
                            selector,
                            _video_quality_for_selector(info, selector),
                            estimate if estimate is not None else "unknown",
                            format_time_range(effective_range)
                            if effective_range is not None
                            else "full",
                        )
                    if effective_range is not None:
                        logger.info(
                            "Clip transfer started: format=%s estimated_bytes=%s range=%s",
                            selector,
                            estimate if estimate is not None else "unknown",
                            format_time_range(effective_range),
                        )
                    try:
                        try:
                            processed = ydl.process_ie_result(
                                copy.deepcopy(extracted), download=True
                            )
                        except yt_dlp.utils.DownloadError as download_exc:
                            if (
                                from_cache
                                and not refreshed
                                and _looks_like_stale_cdn_url(download_exc)
                            ):
                                logger.info(
                                    "Cached extract stale for %s; refreshing before retry",
                                    safe_url_for_log(url),
                                )
                                _evict_extract_info(url)
                                extracted, _ = _extract_info_cached(ydl, url, force_refresh=True)
                                info = _pick_info(extracted)
                                refreshed = True
                                if effective_range is not None:
                                    effective_range = _resolve_clip_range(effective_range, info)
                                    ydl.params["download_ranges"] = _download_ranges_param(
                                        effective_range
                                    )
                                processed = ydl.process_ie_result(
                                    copy.deepcopy(extracted), download=True
                                )
                            else:
                                raise
                        if isinstance(processed, dict):
                            attempt_info = _pick_info(processed)
                        else:
                            attempt_info = info
                        path = _resolve_downloaded_path(attempt_info, attempt_dir, ydl)
                        if not path.exists():
                            raise DownloadError(strings.DOWNLOAD_NO_FILE)
                        if media_format is MediaFormat.AUDIO:
                            path = _convert_audio_for_telegram(
                                path,
                                max_file_bytes=max_file_bytes,
                                deadline=deadline,
                                abort_event=abort_event,
                            )
                        size = path.stat().st_size
                        if size <= 0:
                            raise DownloadError(strings.DOWNLOAD_EMPTY_FILE)
                        if size > source_limit:
                            raise DownloadError(
                                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                                    max_mb=source_limit // (1024 * 1024)
                                )
                            )
                        if size <= max_file_bytes:
                            if media_format is MediaFormat.VIDEO:
                                path = _optimize_video_file(
                                    path,
                                    max_file_bytes=max_file_bytes,
                                    deadline=deadline,
                                    abort_event=abort_event,
                                    on_optimizing=on_optimizing,
                                )
                            if is_clip:
                                logger.info(
                                    "Clip transfer and processing took %.1fs for %s",
                                    time.monotonic() - transfer_started_at,
                                    safe_url_for_log(url),
                                )
                            result_path = work_dir / f"selected{path.suffix}"
                            path.replace(result_path)
                            return DownloadedMedia(
                                path=result_path,
                                title=str(attempt_info.get("title") or title)[:64],
                                kind=(
                                    MediaKind.AUDIO
                                    if media_format is MediaFormat.AUDIO
                                    else _classify(result_path)
                                ),
                                duration=duration,
                            )
                        preserved = work_dir / f"oversized_{attempt}{path.suffix}"
                        path.replace(preserved)
                        if best_oversized is None or size < best_oversized[1]:
                            if best_oversized is not None:
                                best_oversized[0].unlink(missing_ok=True)
                            best_oversized = (preserved, size)
                        else:
                            preserved.unlink(missing_ok=True)
                    except (yt_dlp.utils.DownloadError, DownloadError) as exc:
                        last_error = exc
                        if slow_source.is_set():
                            logger.info(
                                "Video format %s exceeded the %ds estimated download target; "
                                "trying a lower quality",
                                selector,
                                max_estimated_download_seconds,
                            )
                        if not source_too_large.is_set() and not isinstance(exc, DownloadError):
                            message = str(exc).split("\n")[-1].strip()
                            logger.debug(
                                "Format candidate %s failed for %s: %s",
                                selector,
                                safe_url_for_log(url),
                                message,
                            )
                    finally:
                        _cleanup_dir(attempt_dir)

                if best_oversized is not None and media_format is MediaFormat.VIDEO:
                    try:
                        optimized_path = _optimize_video_file(
                            best_oversized[0],
                            max_file_bytes=max_file_bytes,
                            deadline=deadline,
                            abort_event=abort_event,
                            on_optimizing=on_optimizing,
                        )
                    except DownloadError as exc:
                        if isinstance(exc, VideoUnavailableError):
                            raise
                        if quality_policy is VideoQualityPolicy.BEST:
                            raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from exc
                        raise
                    return DownloadedMedia(
                        path=optimized_path,
                        title=title,
                        kind=_classify(optimized_path),
                        duration=duration,
                    )
                if isinstance(last_error, DownloadError):
                    if isinstance(last_error, VideoUnavailableError):
                        raise last_error
                    if quality_policy is VideoQualityPolicy.BEST:
                        raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from last_error
                    raise last_error
                if last_error is not None:
                    if (
                        media_format is MediaFormat.VIDEO
                        and quality_policy is VideoQualityPolicy.BEST
                    ):
                        raise DownloadError(strings.DOWNLOAD_BEST_QUALITY_FAILED) from last_error
                    message = str(last_error).split("\n")[-1].strip()
                    if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
                        raise DownloadError(strings.AUDIO_UNAVAILABLE) from last_error
                    raise DownloadError(
                        strings.DOWNLOAD_FAILED_GENERIC,
                        retryable=_is_transient_download_error(message),
                    ) from last_error
                raise DownloadError(strings.DOWNLOAD_NO_FILE)
    except DownloadError:
        _cleanup_dir(work_dir)
        raise
    except yt_dlp.utils.DownloadError as exc:
        _cleanup_dir(work_dir)
        message = str(exc).split("\n")[-1].strip() or strings.DOWNLOAD_FAILED_GENERIC
        if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
            logger.info("No compatible audio stream available for %s", safe_url_for_log(url))
            raise DownloadError(strings.AUDIO_UNAVAILABLE) from exc
        logger.warning("yt-dlp download error for %s: %s", safe_url_for_log(url), message)
        if "File is larger than max-filesize" in message or "filesize" in message.lower():
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=max_file_bytes // (1024 * 1024))
            ) from exc
        raise DownloadError(
            strings.DOWNLOAD_FAILED_GENERIC,
            retryable=_is_transient_download_error(message),
        ) from exc
    except Exception as exc:
        _cleanup_dir(work_dir)
        message = str(exc).split("\n")[-1].strip() or strings.DOWNLOAD_FAILED_GENERIC
        if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(message):
            logger.info("No compatible audio stream available for %s", safe_url_for_log(url))
            raise DownloadError(strings.AUDIO_UNAVAILABLE) from exc
        logger.warning("Download failed for %s: %s", safe_url_for_log(url), message)
        raise DownloadError(
            strings.DOWNLOAD_FAILED_GENERIC,
            retryable=_is_transient_download_error(message),
        ) from exc


def _cleanup_dir(directory: Path) -> None:
    if not directory.exists():
        return
    for child in directory.glob("**/*"):
        if child.is_file():
            child.unlink(missing_ok=True)
    for child in sorted(directory.glob("**/*"), reverse=True):
        if child.is_dir():
            with contextlib.suppress(OSError):
                child.rmdir()
    with contextlib.suppress(OSError):
        directory.rmdir()


def _cleanup_finished_download(
    worker: asyncio.Task[DownloadedMedia], work_dir_holder: list[Path]
) -> None:
    """Clean an aborted worker's files only after its thread has stopped."""
    try:
        worker.result()
    except (asyncio.CancelledError, Exception):
        pass

    if work_dir_holder:
        try:
            _cleanup_dir(work_dir_holder[0])
        except OSError:
            logger.debug("Could not clean completed download directory", exc_info=True)


async def download_media(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
    allowed_hosts: frozenset[str] | None = None,
    *,
    https_only: bool = False,
    slideshow_slide_ms: int = 2500,
    slideshow_max_images: int = 35,
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    time_range: TimeRange | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
    max_estimated_download_seconds: int = DEFAULT_MAX_ESTIMATED_DOWNLOAD_SECONDS,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    on_optimizing: Callable[[], None] | None = None,
) -> DownloadedMedia:
    if not is_allowed_media_host(url, allowed_hosts):
        raise DownloadError(strings.DOWNLOAD_HOST_NOT_ALLOWED)
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    abort_event = threading.Event()
    work_dir_holder: list[Path] = []
    worker = asyncio.create_task(
        asyncio.to_thread(
            _download_sync,
            url,
            download_dir,
            max_file_bytes,
            timeout_seconds,
            abort_event,
            work_dir_holder,
            https_only=https_only,
            allowed_hosts=allowed_hosts,
            slideshow_slide_ms=slideshow_slide_ms,
            slideshow_max_images=slideshow_max_images,
            slideshow_images_loop=slideshow_images_loop,
            time_range=time_range,
            media_format=media_format,
            quality_policy=quality_policy,
            max_estimated_download_seconds=max_estimated_download_seconds,
            max_media_duration_seconds=max_media_duration_seconds,
            on_optimizing=on_optimizing,
        )
    )
    try:
        completed, _ = await asyncio.wait({worker}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        abort_event.set()
        worker.add_done_callback(lambda task: _cleanup_finished_download(task, work_dir_holder))
        raise

    if worker not in completed:
        abort_event.set()
        worker.add_done_callback(lambda task: _cleanup_finished_download(task, work_dir_holder))
        raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds))
    return worker.result()


def cleanup_media(media: DownloadedMedia) -> None:
    path = media.path
    parent = path.parent
    path.unlink(missing_ok=True)
    if parent.name and parent != path:
        _cleanup_dir(parent)


def cleanup_stale_downloads(
    download_dir: Path,
    max_age_seconds: int = 3600,
) -> int:
    """Remove abandoned download subdirectories older than max_age_seconds.

    Returns the number of directories removed. Top-level files (e.g. the cache DB)
    are left untouched.
    """
    if not download_dir.exists() or not download_dir.is_dir():
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for child in download_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        logger.info("Removing stale download directory: %s", child)
        _cleanup_dir(child)
        removed += 1
    return removed


def _extract_direct_stream_sync(
    url: str,
    max_file_bytes: int,
    *,
    media_format: MediaFormat = MediaFormat.VIDEO,
    https_only: bool = False,
) -> DirectMediaStream | None:
    if https_only and not is_https_url(url):
        return None
    if not is_safe_media_url(url, https_only=https_only):
        return None

    # Photo posts have no playable video stream — skip so callers fall back to
    # the slideshow compiler in download_media.
    from telegram_share_bot.slideshow import detect_tiktok_photo_post

    if detect_tiktok_photo_post(url) is not None:
        return None

    ydl_opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
    }
    try:
        with _safe_dns_resolution():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                extracted, _from_cache = _extract_info_cached(ydl, url)
                info = _pick_info(extracted)

                title = str(info.get("title") or "")[:64]
                duration_raw = info.get("duration")
                duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None

                direct = info.get("url")
                selected_size = info.get("filesize")
                if (
                    isinstance(direct, str)
                    and direct.startswith("http")
                    and is_safe_media_url(direct, https_only=https_only)
                    and ".m3u8" not in direct
                    and ".mpd" not in direct
                    and (
                        info.get("vcodec") == "none"
                        if media_format is MediaFormat.AUDIO
                        else info.get("vcodec") not in (None, "none")
                    )
                    and (
                        info.get("acodec") not in (None, "none")
                        if media_format is MediaFormat.AUDIO
                        else True
                    )
                    and (
                        str(info.get("ext") or "").lower() in {"mp3", "m4a"}
                        if media_format is MediaFormat.AUDIO
                        else True
                    )
                    and isinstance(selected_size, (int, float))
                    and 0 < selected_size <= max_file_bytes
                ):
                    ext = str(info.get("ext") or "mp4")
                    kind = _classify_ext(ext)
                    if kind is not MediaKind.DOCUMENT:
                        return DirectMediaStream(
                            direct_url=direct,
                            title=title,
                            kind=kind,
                            duration=duration,
                            size_bytes=int(selected_size),
                        )

                formats = info.get("formats") or []
                for f in reversed(formats):
                    if not isinstance(f, dict):
                        continue
                    u = f.get("url")
                    if (
                        not isinstance(u, str)
                        or not u.startswith("http")
                        or not is_safe_media_url(u, https_only=https_only)
                    ):
                        continue
                    if ".m3u8" in u or ".mpd" in u:
                        continue
                    proto = f.get("protocol")
                    if https_only:
                        if proto != "https":
                            continue
                    elif proto not in ("http", "https"):
                        continue
                    ext = str(f.get("ext") or "")
                    kind = _classify_ext(ext)
                    if kind is MediaKind.DOCUMENT:
                        continue
                    if media_format is MediaFormat.AUDIO:
                        if f.get("vcodec") != "none" or f.get("acodec") in (None, "none"):
                            continue
                        if ext.lower().lstrip(".") not in {"mp3", "m4a"}:
                            continue
                    elif f.get("vcodec") in (None, "none"):
                        continue
                    size = f.get("filesize")
                    if not isinstance(size, (int, float)) or not 0 < size <= max_file_bytes:
                        continue
                    return DirectMediaStream(
                        direct_url=u,
                        title=title,
                        kind=kind,
                        duration=duration,
                        size_bytes=int(size),
                    )
    except Exception as exc:
        logger.debug(
            "Direct stream extraction skipped or failed for %s: %s",
            safe_url_for_log(url),
            exc,
        )
    return None


async def get_direct_stream(
    url: str,
    max_file_bytes: int,
    timeout_seconds: int = 15,
    allowed_hosts: frozenset[str] | None = None,
    *,
    media_format: MediaFormat = MediaFormat.VIDEO,
    https_only: bool = False,
) -> DirectMediaStream | None:
    if not is_allowed_media_host(url, allowed_hosts):
        return None
    if https_only and not is_https_url(url):
        return None
    if not is_safe_media_url(url, https_only=https_only):
        return None

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _extract_direct_stream_sync,
                url,
                max_file_bytes,
                media_format=media_format,
                https_only=https_only,
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return None
