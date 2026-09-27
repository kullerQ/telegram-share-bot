"""Format ranking and selector helpers for yt-dlp media downloads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.media.models import (
    DownloadError,
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)

_SOURCE_SIZE_MULTIPLIER = 2
_MAX_FORMAT_ATTEMPTS = 4
_QUALITY_SAMPLE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _FormatCandidate:
    selector: str
    quality: tuple[float, float, float]
    estimated_size: int | None
    clip_transport_priority: int = 0
    codec_priority: int = 0


def _projected_transfer_seconds(elapsed: float, downloaded: int, total: int) -> float | None:
    """Project full transfer time from measured bytes and elapsed wall time."""
    if elapsed <= 0 or downloaded <= 0 or total <= 0:
        return None
    return elapsed * max(total, downloaded) / downloaded


def _format_bytes(
    format_info: dict[str, Any],
    duration_scale: float,
    fallback_duration: float | None = None,
) -> int | None:
    for key in ("filesize", "filesize_approx"):
        size = format_info.get(key)
        if isinstance(size, (int, float)) and size > 0:
            return max(1, int(size * duration_scale))
    bitrate = format_info.get("tbr") or format_info.get("abr")
    duration = format_info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = fallback_duration
        duration_scale = 1.0
    if (
        isinstance(bitrate, (int, float))
        and bitrate > 0
        and isinstance(duration, (int, float))
        and duration > 0
    ):
        return max(1, int(float(bitrate) * 1000 * float(duration) * duration_scale / 8))
    return None


def _video_quality_label(format_info: dict[str, Any]) -> str:
    """Format known progressive video quality details for safe operational logs."""
    height = format_info.get("height")
    fps = format_info.get("fps")
    video_codec = format_info.get("vcodec")
    height_label = (
        f"{int(height)}p"
        if isinstance(height, (int, float)) and height > 0
        else "unknown-resolution"
    )
    fps_label = f"{float(fps):g}fps" if isinstance(fps, (int, float)) and fps > 0 else "unknown-fps"
    codec_label = "unknown-codec"
    if isinstance(video_codec, str):
        codec_prefix = re.split(r"[.\s]", video_codec, maxsplit=1)[0]
        if re.fullmatch(r"[A-Za-z0-9_-]+", codec_prefix):
            codec_label = codec_prefix
    return f"{height_label} {fps_label} {codec_label}"


def _video_quality_for_selector(info: dict[str, Any], selector: str) -> str:
    """Return resolution, frame rate, and codec for the selector's video stream."""
    formats = info.get("formats")
    video_format_id = selector.split("+", maxsplit=1)[0]
    if isinstance(formats, list):
        for item in formats:
            if isinstance(item, dict) and item.get("format_id") == video_format_id:
                return _video_quality_label(item)
    return "unknown-resolution unknown-fps unknown-codec"


def _format_candidates(
    info: dict[str, Any],
    *,
    max_file_bytes: int,
    duration_scale: float = 1.0,
    quality_policy: VideoQualityPolicy = VideoQualityPolicy.AUTO,
) -> list[_FormatCandidate]:
    """Rank bounded sources by quality or proximity to the balanced tier."""
    formats = info.get("formats")
    if not isinstance(formats, list):
        return []
    usable = [item for item in formats if isinstance(item, dict)]
    source_duration = info.get("duration")
    fallback_duration = (
        float(source_duration) * duration_scale
        if isinstance(source_duration, (int, float)) and source_duration > 0
        else None
    )

    def format_id(item: dict[str, Any]) -> str | None:
        value = item.get("format_id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            return value
        return None

    audio_only = [
        item
        for item in usable
        if item.get("acodec") not in (None, "none")
        and item.get("vcodec") == "none"
        and format_id(item) is not None
    ]
    best_audio = max(
        audio_only,
        key=lambda item: float(item.get("abr") or item.get("tbr") or 0),
        default=None,
    )
    candidates: dict[str, _FormatCandidate] = {}
    for item in usable:
        identifier = format_id(item)
        if identifier is None or item.get("vcodec") in (None, "none"):
            continue
        has_audio = item.get("acodec") not in (None, "none")
        selector = identifier
        estimated = _format_bytes(item, duration_scale, fallback_duration)
        if not has_audio:
            if best_audio is None:
                continue
            audio_id = format_id(best_audio)
            if audio_id is None:
                continue
            selector = f"{identifier}+{audio_id}"
            audio_size = _format_bytes(best_audio, duration_scale, fallback_duration)
            if estimated is not None and audio_size is not None:
                estimated += audio_size
            elif estimated is not None:
                estimated = None
        if (
            estimated is not None
            and estimated > max_file_bytes * _SOURCE_SIZE_MULTIPLIER
            and quality_policy is not VideoQualityPolicy.BEST
        ):
            continue
        height = item.get("height")
        fps = item.get("fps")
        bitrate = item.get("tbr") or item.get("vbr")
        quality = (
            float(height) if isinstance(height, (int, float)) else 0.0,
            float(fps) if isinstance(fps, (int, float)) else 0.0,
            float(bitrate) if isinstance(bitrate, (int, float)) else 0.0,
        )
        protocol = str(item.get("protocol") or "")
        clip_transport_priority = (
            0 if duration_scale >= 1.0 or protocol in {"m3u8", "m3u8_native"} else 1
        )
        candidates[selector] = _FormatCandidate(
            selector,
            quality,
            estimated,
            clip_transport_priority,
            0 if str(item.get("vcodec") or "").lower().startswith(("avc1", "h264")) else 1,
        )

    def rank(candidate: _FormatCandidate) -> tuple[float, ...]:
        if quality_policy is VideoQualityPolicy.BALANCED:
            return (
                0
                if candidate.quality[0] == 720
                and abs(candidate.quality[1] - 60) <= 1
                and candidate.codec_priority == 0
                else 1,
                -candidate.quality[0],
                -candidate.quality[1],
                candidate.estimated_size is not None and candidate.estimated_size > max_file_bytes,
                -candidate.quality[2],
            )
        return (
            candidate.clip_transport_priority,
            -candidate.quality[0],
            -candidate.quality[1],
            -candidate.quality[2],
            candidate.codec_priority,
        )

    ranked = sorted(candidates.values(), key=rank)
    if quality_policy is VideoQualityPolicy.BEST:
        return ranked[:1]
    return ranked[:_MAX_FORMAT_ATTEMPTS]


def _audio_format_candidates(
    info: dict[str, Any],
    *,
    max_file_bytes: int,
    duration_scale: float = 1.0,
) -> list[_FormatCandidate]:
    """Rank audio-only source formats by bitrate while avoiding implausible sizes."""
    formats = info.get("formats")
    if not isinstance(formats, list):
        return []
    source_duration = info.get("duration")
    fallback_duration = (
        float(source_duration) * duration_scale
        if isinstance(source_duration, (int, float)) and source_duration > 0
        else None
    )
    candidates: dict[str, _FormatCandidate] = {}
    source_limit = max_file_bytes * _SOURCE_SIZE_MULTIPLIER
    for item in formats:
        if not isinstance(item, dict):
            continue
        identifier = item.get("format_id")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
            or item.get("vcodec") != "none"
            or item.get("acodec") in (None, "none")
        ):
            continue
        estimated = _format_bytes(item, duration_scale, fallback_duration)
        if estimated is not None and estimated > source_limit:
            continue
        bitrate = item.get("abr") or item.get("tbr")
        quality = (
            0.0,
            0.0,
            float(bitrate) if isinstance(bitrate, (int, float)) else 0.0,
        )
        candidates[identifier] = _FormatCandidate(identifier, quality, estimated)
    return sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.estimated_size is not None and candidate.estimated_size > max_file_bytes,
            -candidate.quality[2],
        ),
    )[:_MAX_FORMAT_ATTEMPTS]


def _set_attempt_format_selector(ydl: yt_dlp.YoutubeDL, selector: str) -> None:
    """Rebuild yt-dlp's cached selector after changing the requested format."""
    ydl.params["format"] = selector
    ydl.format_selector = ydl.build_format_selector(selector)


def _set_attempt_output_template(ydl: yt_dlp.YoutubeDL, template: str) -> None:
    """Update yt-dlp's default output template without discarding its type mapping."""
    current = ydl.params.get("outtmpl")
    templates = dict(current) if isinstance(current, dict) else {}
    templates["default"] = template
    ydl.params["outtmpl"] = templates


def _is_audio_unavailable_error(error: BaseException | str) -> bool:
    """Recognize extractor errors that mean a link has no usable audio."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "requested format is not available",
            "no video formats found",
            "no audio formats found",
            "only images are available",
        )
    )


def _default_audio_selector(time_range: TimeRange | None, source_limit: int) -> str:
    if time_range is not None:
        return "ba"
    return f"ba[filesize<{source_limit}]/ba[filesize_approx<{source_limit}]/ba"


def _default_video_selector(time_range: TimeRange | None, source_limit: int) -> str:
    if time_range is not None:
        return "bv*+ba/b"
    return (
        f"bv*[filesize<{source_limit}]+ba/"
        f"b[filesize<{source_limit}]/"
        f"bv*[filesize_approx<{source_limit}]+ba/"
        f"b[filesize_approx<{source_limit}]/"
        "b/"
        "bv*[acodec=none][protocol=https]/"
        "bv*[acodec=none]"
    )


def select_download_candidates(
    info: dict[str, Any],
    *,
    time_range: TimeRange | None,
    media_format: MediaFormat,
    quality_policy: VideoQualityPolicy,
    max_file_bytes: int,
    source_limit: int,
) -> tuple[float, list[_FormatCandidate], list[str]]:
    """Choose ranked source formats and the fallback selector for one request."""
    duration_scale = 1.0
    source_duration = info.get("duration")
    if (
        time_range is not None
        and time_range.duration_seconds is not None
        and isinstance(source_duration, (int, float))
        and source_duration > 0
    ):
        duration_scale = min(
            1.0, time_range.duration_seconds / float(source_duration)
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
        _default_audio_selector(time_range, source_limit)
        if media_format is MediaFormat.AUDIO
        else _default_video_selector(time_range, source_limit)
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
    return duration_scale, candidates, selectors
