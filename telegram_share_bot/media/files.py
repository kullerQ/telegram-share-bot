"""Downloaded file classification, resolution, and cleanup helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.media.models import DownloadError, MediaKind

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".temp", ".aria2"}


def _classify(path: Path) -> MediaKind:
    return _classify_ext(path.suffix)


def _classify_ext(ext: str) -> MediaKind:
    suffix = f".{ext.lower().lstrip('.')}"
    if suffix == ".gif":
        return MediaKind.ANIMATION
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


def _cleanup_dir(directory: Path) -> None:
    if directory.is_symlink() or not directory.exists():
        return
    try:
        children = list(directory.glob("**/*"))
    except OSError:
        logger.warning("Could not enumerate media cleanup directory")
        return
    failed = False
    for child in children:
        if child.is_symlink() or child.is_file():
            try:
                child.unlink(missing_ok=True)
            except OSError:
                failed = True
    for child in sorted(children, reverse=True):
        if child.is_dir() and not child.is_symlink():
            try:
                child.rmdir()
            except OSError:
                failed = True
    try:
        directory.rmdir()
    except OSError:
        failed = True
    if failed:
        logger.warning("Media cleanup left one or more locked or inaccessible files")
