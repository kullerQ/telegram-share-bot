"""ffmpeg slideshow composition and MP4 rendering."""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
    DEFAULT_SLIDESHOW_SLIDE_MS,
)
from telegram_share_bot.media.duration import ensure_full_media_duration
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaKind
from telegram_share_bot.media.network import create_youtube_dl
from telegram_share_bot.media.security import _safe_dns_resolution
from telegram_share_bot.platforms.urls import safe_url_for_log
from telegram_share_bot.tiktok.assets import (
    _download_bytes,
    _guess_ext,
    _probe_media_duration,
)
from telegram_share_bot.tiktok.source import SlideshowSource
from telegram_share_bot.tiktok.timeline import (
    _build_nav_overlays,
    _cycle_images,
    plan_slideshow_timeline,
)

logger = logging.getLogger(__name__)

def _build_ffmpeg_argv(
    *,
    ffmpeg_bin: str,
    image_paths: list[Path],
    audio_path: Path | None,
    output_path: Path,
    slide_durations: list[float],
    total: float,
    width: int,
    height: int,
    crf: int,
    unique_image_count: int | None = None,
    nav_overlay_paths: list[Path] | None = None,
) -> list[str]:
    if len(image_paths) != len(slide_durations):
        raise ValueError("image_paths and slide_durations length mismatch")

    unique_n = unique_image_count if unique_image_count is not None else len(image_paths)
    nav_paths = nav_overlay_paths or []
    use_nav = unique_n >= 2 and len(nav_paths) == unique_n

    argv: list[str] = [ffmpeg_bin, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    n = len(image_paths)
    for path, slide_t in zip(image_paths, slide_durations, strict=True):
        argv.extend(["-loop", "1", "-t", f"{slide_t:.3f}", "-i", str(path)])
    for nav_path in nav_paths if use_nav else []:
        # Loop still overlays for the full slide duration.
        argv.extend(["-loop", "1", "-i", str(nav_path)])
    if audio_path is not None:
        # Play the full audio once (video length is planned to match).
        argv.extend(["-i", str(audio_path)])

    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        "in_range=pc:out_range=tv,"
        f"pad={width}:{height}:-1:-1:color=black,setsar=1,fps=30"
    )
    # TikTok places page dots just above the bottom UI chrome.
    nav_bottom_margin = max(36, height // 28)
    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    nav_input_base = n  # first nav overlay input index
    for idx in range(n):
        if use_nav:
            active = idx % unique_n
            nav_in = nav_input_base + active
            filter_parts.append(
                f"[{idx}:v]{scale}[b{idx}];"
                f"[b{idx}][{nav_in}:v]overlay=(W-w)/2:H-h-{nav_bottom_margin}:shortest=1[v{idx}]"
            )
        else:
            filter_parts.append(f"[{idx}:v]{scale}[v{idx}]")
        concat_inputs.append(f"[v{idx}]")
    filter_parts.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[v]")
    filter_complex = ";".join(filter_parts)

    argv.extend(["-filter_complex", filter_complex, "-map", "[v]"])
    audio_input_index = n + (unique_n if use_nav else 0)
    if audio_path is not None:
        argv.extend(["-map", f"{audio_input_index}:a", "-c:a", "aac", "-b:a", "128k"])
    else:
        argv.extend(["-an"])
    argv.extend(
        [
            "-t",
            f"{total:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return argv


def _run_ffmpeg(
    argv: list[str],
    *,
    timeout_seconds: float,
    abort_event: threading.Event | None,
) -> None:
    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=max(0.1, timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED) from exc
    except OSError as exc:
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED) from exc

    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")[-500:]
        logger.warning("ffmpeg slideshow failed (code %s): %s", completed.returncode, stderr)
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED)


def build_slideshow_video(
    source: SlideshowSource,
    work_dir: Path,
    *,
    max_file_bytes: int,
    timeout_seconds: int,
    slide_ms: int = DEFAULT_SLIDESHOW_SLIDE_MS,
    images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    abort_event: threading.Event | None = None,
    https_only: bool = False,
    max_media_duration_seconds: int = DEFAULT_MAX_MEDIA_DURATION_SECONDS,
) -> DownloadedMedia:
    """Download slides (+ optional audio) and mux into an MP4 via ffmpeg."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise DownloadError(strings.SLIDESHOW_FFMPEG_MISSING)

    if not source.image_urls:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    per_slide = max(0.5, slide_ms / 1000.0)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
    }

    image_paths: list[Path] = []
    audio_path: Path | None = None
    bytes_used = 0
    started = time.monotonic()

    def _remaining_timeout() -> float:
        return max(0.1, float(timeout_seconds) - (time.monotonic() - started))

    try:
        with _safe_dns_resolution():
            with create_youtube_dl(ydl_opts) as ydl:
                for idx, image_url in enumerate(source.image_urls):
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
                    if https_only and not image_url.lower().startswith("https://"):
                        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
                    ext = _guess_ext(image_url, "jpg")
                    dest = work_dir / f"slide_{idx:03d}.{ext}"
                    written = _download_bytes(
                        ydl,
                        image_url,
                        dest,
                        remaining_budget=max_file_bytes - bytes_used,
                        abort_event=abort_event,
                    )
                    bytes_used += written
                    image_paths.append(dest)

                if source.audio_url:
                    if https_only and not source.audio_url.lower().startswith("https://"):
                        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
                    audio_ext = _guess_ext(source.audio_url, "mp3")
                    audio_dest = work_dir / f"audio.{audio_ext}"
                    written = _download_bytes(
                        ydl,
                        source.audio_url,
                        audio_dest,
                        remaining_budget=max_file_bytes - bytes_used,
                        abort_event=abort_event,
                    )
                    bytes_used += written
                    audio_path = audio_dest
    except DownloadError:
        raise
    except Exception as exc:
        logger.warning(
            "Slideshow asset download failed for %s: %s",
            safe_url_for_log(source.canonical_url),
            exc,
        )
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    audio_duration = source.audio_duration
    if audio_path is not None and (audio_duration is None or audio_duration <= 0):
        audio_duration = _probe_media_duration(audio_path)

    total, slide_durations = plan_slideshow_timeline(
        len(image_paths),
        per_slide,
        audio_duration if audio_path is not None else None,
        images_loop=images_loop,
    )
    ensure_full_media_duration(total, max_media_duration_seconds)
    sequenced_images = _cycle_images(image_paths, len(slide_durations))
    unique_count = len(image_paths)
    # Half-up so fractional seconds round sensibly for Telegram's int duration.
    duration = max(1, math.floor(total + 0.5))

    output_path = work_dir / "slideshow.mp4"
    # One source render; the shared downloader applies at most two bounded
    # Telegram-size optimization passes if this output is still too large.
    encode_attempts: list[tuple[int, int, int]] = [(1080, 1920, 24)]

    last_error: Exception | None = None
    for width, height, crf in encode_attempts:
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        output_path.unlink(missing_ok=True)
        try:
            nav_overlays = _build_nav_overlays(
                work_dir,
                unique_count=unique_count,
                frame_width=width,
            )
        except Exception as exc:
            logger.warning("Could not build slideshow nav dots: %s", exc)
            nav_overlays = []
        argv = _build_ffmpeg_argv(
            ffmpeg_bin=ffmpeg_bin,
            image_paths=sequenced_images,
            audio_path=audio_path,
            output_path=output_path,
            slide_durations=slide_durations,
            total=total,
            width=width,
            height=height,
            crf=crf,
            unique_image_count=unique_count,
            nav_overlay_paths=nav_overlays,
        )
        try:
            _run_ffmpeg(
                argv,
                timeout_seconds=_remaining_timeout(),
                abort_event=abort_event,
            )
        except DownloadError as exc:
            last_error = exc
            continue

        if not output_path.exists() or output_path.stat().st_size <= 0:
            last_error = DownloadError(strings.SLIDESHOW_BUILD_FAILED)
            continue

        size = output_path.stat().st_size
        if size > max_file_bytes:
            logger.info(
                "Slideshow source output %s bytes exceeds bounded source limit",
                size,
            )
            last_error = DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            )
            continue

        return DownloadedMedia(
            path=output_path,
            title=source.title,
            kind=MediaKind.VIDEO,
            duration=duration,
        )

    if last_error is not None:
        raise last_error
    raise DownloadError(strings.SLIDESHOW_BUILD_FAILED)
