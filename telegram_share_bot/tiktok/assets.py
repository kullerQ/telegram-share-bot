"""Bounded downloading and duration probing for slideshow media assets."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.impersonate import ImpersonateTarget

from telegram_share_bot import strings
from telegram_share_bot.media.models import DownloadError

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def _guess_ext(url: str, default: str) -> str:
    path = urlsplit(url).path.lower()
    for ext in _IMAGE_EXTENSIONS | {".mp3", ".m4a", ".aac", ".mp4", ".wav"}:
        if path.endswith(ext):
            return ext.lstrip(".")
    # TikTok CDN image URLs often omit a file extension.
    if "photomode" in path or "image" in path:
        return "jpg"
    return default


def _download_bytes(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    dest: Path,
    *,
    remaining_budget: int,
    abort_event: threading.Event | None,
) -> int:
    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    if remaining_budget <= 0:
        raise DownloadError(
            strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=1)
        )

    request = Request(
        url,
        extensions={"impersonate": ImpersonateTarget("chrome")},
    )
    try:
        response = ydl.urlopen(request)  # type: ignore[arg-type]
    except Exception as exc:
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    written = 0
    try:
        with dest.open("wb") as out, response:
            while True:
                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining_budget:
                    raise DownloadError(
                        strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                            max_mb=max(1, remaining_budget // (1024 * 1024))
                        )
                    )
                out.write(chunk)
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    if written <= 0:
        dest.unlink(missing_ok=True)
        raise DownloadError(strings.DOWNLOAD_EMPTY_FILE)
    return written

_FFMPEG_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _duration_from_mutagen(path: Path) -> float | None:
    """Return media duration via mutagen, or None on failure."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return None
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return None
    if audio is None:
        return None
    info = getattr(audio, "info", None)
    length = getattr(info, "length", None) if info is not None else None
    if isinstance(length, (int, float)) and length > 0:
        return float(length)
    return None


def _duration_from_ffmpeg(path: Path) -> float | None:
    """Parse duration from ``ffmpeg -i`` stderr (no ffprobe required)."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(path)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    match = _FFMPEG_DURATION_RE.search(stderr)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    try:
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
    return value if value > 0 else None


def _probe_media_duration(path: Path) -> float | None:
    """Return media duration in seconds, or None on failure.

    Prefers mutagen (already a yt-dlp[default] dep); falls back to parsing
    ``ffmpeg -i`` stderr so the image need not ship ffprobe.
    """
    return _duration_from_mutagen(path) or _duration_from_ffmpeg(path)
