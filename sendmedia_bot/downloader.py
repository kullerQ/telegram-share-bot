"""Download media URLs with yt-dlp under size and time limits."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yt_dlp

from sendmedia_bot import strings

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    title: str
    kind: MediaKind
    duration: int | None


class DownloadError(Exception):
    """Raised when a URL cannot be downloaded within bot limits."""


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text.strip())
    if match is None:
        return None
    return match.group(0).rstrip(").,]}>'\"")


def _classify(path: Path) -> MediaKind:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if suffix in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    return MediaKind.DOCUMENT


def _resolve_downloaded_path(info: Any, work_dir: Path, ydl: yt_dlp.YoutubeDL) -> Path:
    requested = info.get("requested_downloads") if isinstance(info, dict) else None
    if isinstance(requested, list) and requested:
        first = requested[0]
        if isinstance(first, dict):
            filepath = first.get("filepath")
            if isinstance(filepath, str) and filepath:
                return Path(filepath)

    prepared = Path(ydl.prepare_filename(info))
    if prepared.exists():
        return prepared

    files = [p for p in work_dir.glob("*") if p.is_file()]
    if not files:
        raise DownloadError(strings.DOWNLOAD_NO_FILE)
    return max(files, key=lambda p: p.stat().st_size)


def _pick_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)
    if "entries" not in info:
        return cast(dict[str, Any], info)
    raw_entries = info.get("entries")
    if raw_entries is None:
        raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
    entries = [entry for entry in list(raw_entries) if isinstance(entry, dict)]
    if not entries:
        raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
    return cast(dict[str, Any], entries[0])


def _download_sync(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
) -> DownloadedMedia:
    work_dir = download_dir / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(work_dir / "%(title).80B [%(id)s].%(ext)s")

    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
        "retries": 2,
        "max_filesize": max_file_bytes,
        "format": (
            f"bv*[filesize<{max_file_bytes}]+ba/"
            f"b[filesize<{max_file_bytes}]/"
            f"bv*[filesize_approx<{max_file_bytes}]+ba/"
            f"b[filesize_approx<{max_file_bytes}]/"
            "bv*+ba/b"
        ),
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            extracted = ydl.extract_info(url, download=True)
            if not isinstance(extracted, dict):
                raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)

            info = _pick_info(extracted)
            path = _resolve_downloaded_path(info, work_dir, ydl)
            if not path.exists():
                raise DownloadError(strings.DOWNLOAD_NO_FILE)

            size = path.stat().st_size
            if size <= 0:
                raise DownloadError(strings.DOWNLOAD_EMPTY_FILE)
            if size > max_file_bytes:
                path.unlink(missing_ok=True)
                raise DownloadError(
                    strings.DOWNLOAD_TOO_LARGE.format(
                        size_mb=size // (1024 * 1024),
                        max_mb=max_file_bytes // (1024 * 1024),
                    )
                )

            title = str(info.get("title") or path.stem)[:64]
            duration_raw = info.get("duration")
            duration = (
                int(duration_raw) if isinstance(duration_raw, (int, float)) else None
            )

            return DownloadedMedia(
                path=path,
                title=title,
                kind=_classify(path),
                duration=duration,
            )
    except DownloadError:
        _cleanup_dir(work_dir)
        raise
    except yt_dlp.utils.DownloadError as exc:
        _cleanup_dir(work_dir)
        message = str(exc).split("\n")[-1].strip() or strings.DOWNLOAD_FAILED_GENERIC
        if "File is larger than max-filesize" in message or "filesize" in message.lower():
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            ) from exc
        raise DownloadError(message) from exc
    except Exception as exc:
        _cleanup_dir(work_dir)
        raise DownloadError(
            strings.DOWNLOAD_FAILED_WITH_DETAIL.format(error=exc)
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


async def download_media(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
) -> DownloadedMedia:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _download_sync,
                url,
                download_dir,
                max_file_bytes,
                timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise DownloadError(
            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
        ) from exc


def cleanup_media(media: DownloadedMedia) -> None:
    path = media.path
    parent = path.parent
    path.unlink(missing_ok=True)
    if parent.name and parent != path:
        _cleanup_dir(parent)
