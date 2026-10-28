"""Bounded loudness measurement and normalization for Telegram media."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from telegram_share_bot import strings
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaKind

logger = logging.getLogger(__name__)

_TARGET_LUFS = -16.0
_QUIET_THRESHOLD_LUFS = -20.0
_TRUE_PEAK_CEILING_DBTP = -1.5
# Reserve 1 dB for AAC encoder overshoot after loudness adjustment.
_ENCODER_PEAK_MARGIN_DB = 1.0
_MAX_TARGET_INCREASE_LU = 24.0
_AUDIO_BITRATE = "192k"
_MAX_DIAGNOSTIC_BYTES = 64 * 1024


class _NoAudioStream(Exception):
    """FFmpeg confirmed that the input has no audio stream."""


def _target_lufs(input_lufs: float) -> float:
    """Limit quiet-source target loudness to a maximum 24 LU increase."""
    if not math.isfinite(input_lufs):
        raise ValueError("loudness measurement is not finite")
    return min(_TARGET_LUFS, input_lufs + _MAX_TARGET_INCREASE_LU)


def _parse_measurement(stderr: str) -> tuple[float, float, float, float] | None:
    """Extract the loudnorm first-pass JSON object and validate its measurements."""
    for match in re.finditer(r"\{[^{}]*\}", stderr, re.DOTALL):
        try:
            payload = json.loads(match.group())
            input_lufs = float(payload["input_i"])
            true_peak_dbtp = float(payload["input_tp"])
            input_lra = float(payload["input_lra"])
            input_threshold = float(payload["input_thresh"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if input_lufs == float("-inf"):
            return None
        if all(
            math.isfinite(value)
            for value in (input_lufs, true_peak_dbtp, input_lra, input_threshold)
        ):
            return input_lufs, true_peak_dbtp, input_lra, input_threshold
        raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True)
    if re.search(r"does not contain any stream|matches no streams", stderr, re.IGNORECASE):
        return None
    raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True)


def _run_ffmpeg_bounded(
    argv: list[str],
    *,
    output: Path | None,
    max_file_bytes: int,
    deadline: float,
    abort_event: threading.Event | None,
) -> str:
    """Run FFmpeg while bounding duration, output size, and captured diagnostics."""
    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC, retryable=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0), retryable=True)
    try:
        with tempfile.TemporaryFile() as diagnostics:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
            )
            try:
                while True:
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC, retryable=True)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0),
                            retryable=True,
                        )
                    if diagnostics.tell() > _MAX_DIAGNOSTIC_BYTES:
                        raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True)
                    if (
                        output is not None
                        and output.exists()
                        and output.stat().st_size > max_file_bytes
                    ):
                        raise DownloadError(
                            strings.DOWNLOAD_TOO_LARGE.format(
                                size_mb=output.stat().st_size // (1024 * 1024),
                                max_mb=max_file_bytes // (1024 * 1024),
                            ),
                            retryable=True,
                        )
                    try:
                        return_code = process.wait(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            diagnostics.seek(0)
            stderr = diagnostics.read(_MAX_DIAGNOSTIC_BYTES).decode("utf-8", errors="replace")
        if return_code != 0:
            if output is None and re.search(
                r"does not contain any stream|matches no streams", stderr, re.IGNORECASE
            ):
                raise _NoAudioStream
            raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True)
        if output is not None:
            if not output.exists() or output.stat().st_size <= 0:
                raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True)
            if output.stat().st_size > max_file_bytes:
                raise DownloadError(
                    strings.DOWNLOAD_TOO_LARGE.format(
                        size_mb=output.stat().st_size // (1024 * 1024),
                        max_mb=max_file_bytes // (1024 * 1024),
                    ),
                    retryable=True,
                )
        return stderr
    except DownloadError:
        raise
    except OSError as exc:
        raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True) from exc


def normalize_quiet_audio(
    media: DownloadedMedia,
    *,
    max_file_bytes: int,
    deadline: float,
    abort_event: threading.Event | None,
) -> DownloadedMedia:
    """Normalize quiet audio in an audio/video file while preserving video bitstreams.

    Documents and animations are returned unchanged. Audio and video media are
    analyzed with FFmpeg loudnorm; loud tracks and tracks without usable audio
    measurements are returned unchanged. A quiet source is capped to a target
    no more than 24 LU above its measured integrated loudness.
    """
    if media.kind not in (MediaKind.AUDIO, MediaKind.VIDEO):
        logger.info("Audio normalization skipped: kind=%s", media.kind.value)
        return media

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_AUDIO_FFMPEG_MISSING, retryable=True)

    measure_argv = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-i",
        str(media.path),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0?",
        "-af",
        f"loudnorm=I={_TARGET_LUFS}:TP={_TRUE_PEAK_CEILING_DBTP}:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        stderr = _run_ffmpeg_bounded(
            measure_argv,
            output=None,
            max_file_bytes=max_file_bytes,
            deadline=deadline,
            abort_event=abort_event,
        )
        measurement = _parse_measurement(stderr)
        if measurement is None:
            logger.info("Audio normalization skipped: no audio stream or silence")
            return media
        input_lufs, true_peak_dbtp, input_lra, input_threshold = measurement
    except _NoAudioStream:
        logger.info("Audio normalization skipped: no audio stream")
        return media
    except DownloadError as exc:
        logger.info("Audio normalization measurement failed")
        raise exc

    if input_lufs >= _QUIET_THRESHOLD_LUFS:
        logger.info("Audio normalization skipped: integrated loudness %.1f LUFS", input_lufs)
        return media
    try:
        target_lufs = _target_lufs(input_lufs)
    except ValueError as exc:
        raise DownloadError(strings.AUDIO_NORMALIZATION_FAILED, retryable=True) from exc
    target_lra = max(11.0, input_lra)
    true_peak_target = _TRUE_PEAK_CEILING_DBTP - _ENCODER_PEAK_MARGIN_DB

    suffix = ".mp4" if media.kind is MediaKind.VIDEO else ".m4a"
    descriptor, output_name = tempfile.mkstemp(
        prefix=f"{media.path.stem}_normalized_", suffix=suffix, dir=media.path.parent
    )
    os.close(descriptor)
    Path(output_name).unlink(missing_ok=True)
    output = Path(output_name)
    output_args = ["-map", "0:a:0", "-sn", "-dn"]
    if media.kind is MediaKind.VIDEO:
        output_args.extend(["-map", "0:v:0?", "-c:v", "copy"])
    else:
        output_args.append("-vn")
    loudnorm_filter = (
        f"loudnorm=I={target_lufs:.2f}:TP={true_peak_target:.1f}:LRA={target_lra:.2f}:"
        f"measured_I={input_lufs:.2f}:measured_TP={true_peak_dbtp:.2f}:"
        f"measured_LRA={input_lra:.2f}:measured_thresh={input_threshold:.2f}:linear=true"
    )
    output_args.extend(
        [
            "-af",
            loudnorm_filter,
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            _AUDIO_BITRATE,
            "-movflags",
            "+faststart",
        ]
    )
    output_args.append(str(output))
    apply_argv = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media.path),
        *output_args,
    ]
    try:
        _run_ffmpeg_bounded(
            apply_argv,
            output=output,
            max_file_bytes=max_file_bytes,
            deadline=deadline,
            abort_event=abort_event,
        )
    except Exception:
        with suppress(OSError):
            output.unlink(missing_ok=True)
        raise

    logger.info(
        "Audio normalization applied: input_loudness=%.1f LUFS true_peak=%.1f dBTP "
        "target=%.1f LUFS",
        input_lufs,
        true_peak_dbtp,
        target_lufs,
    )
    media.path.unlink(missing_ok=True)
    return replace(media, path=output)
