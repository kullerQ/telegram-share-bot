"""Download media URLs with yt-dlp under size and time limits."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import yt_dlp

from sendmedia_bot import strings

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".temp", ".aria2"}


def _is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_safe_ip(ip.ipv4_mapped)

    if str(ip) == "169.254.169.254":
        return False

    return True


def is_safe_media_url(url: str) -> bool:
    """Validate that a URL uses http(s) and does not point to internal/private/loopback/cloud-metadata networks."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        hostname_clean = hostname.strip().lower().rstrip(".")
        if not hostname_clean:
            return False

        if (
            hostname_clean == "localhost"
            or hostname_clean.endswith((".localhost", ".local", ".internal", ".lan"))
        ):
            return False

        try:
            ip = ipaddress.ip_address(hostname_clean)
            return _is_safe_ip(ip)
        except ValueError:
            pass

        addr_info = socket.getaddrinfo(hostname_clean, None, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False

        for res in addr_info:
            sockaddr = res[4]
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if not _is_safe_ip(ip):
                return False

        return True
    except Exception as exc:
        logger.warning("URL security check rejected %s: %s", url, exc)
        return False



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


@dataclass(frozen=True, slots=True)
class DirectMediaStream:
    direct_url: str
    title: str
    kind: MediaKind
    duration: int | None = None


class DownloadError(Exception):
    """Raised when a URL cannot be downloaded within bot limits."""


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text.strip())
    if match is None:
        return None
    return match.group(0).rstrip(").,]}>'\"")


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
    requested = info.get("requested_downloads") if isinstance(info, dict) else None
    if isinstance(requested, list) and requested:
        first = requested[0]
        if isinstance(first, dict):
            filepath = first.get("filepath")
            if isinstance(filepath, str) and filepath:
                p = Path(filepath)
                if p.exists() and _is_complete_file(p):
                    return p

    prepared = Path(ydl.prepare_filename(info))
    if prepared.exists() and _is_complete_file(prepared):
        return prepared

    completed_files = [
        p for p in work_dir.glob("*") if p.is_file() and _is_complete_file(p)
    ]
    if not completed_files:
        partial_files = [
            p for p in work_dir.glob("*") if p.is_file() and not _is_complete_file(p)
        ]
        if partial_files:
            raise DownloadError(strings.DOWNLOAD_FAILED_INCOMPLETE)
        raise DownloadError(strings.DOWNLOAD_NO_FILE)

    return max(completed_files, key=lambda p: p.stat().st_size)


def _pick_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)
    if "entries" not in info:
        picked = cast(dict[str, Any], info)
    else:
        raw_entries = info.get("entries")
        if raw_entries is None:
            raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
        entries = [entry for entry in list(raw_entries) if isinstance(entry, dict)]
        if not entries:
            raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
        picked = cast(dict[str, Any], entries[0])

    if picked.get("is_live") or picked.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError(strings.DOWNLOAD_LIVE_UNSUPPORTED)
    return picked


def _download_sync(
    url: str,
    download_dir: Path,
    max_file_bytes: int,
    timeout_seconds: int,
    abort_event: threading.Event | None = None,
    work_dir_holder: list[Path] | None = None,
) -> DownloadedMedia:
    if not is_safe_media_url(url):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    work_dir = download_dir / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    if work_dir_holder is not None:
        work_dir_holder.append(work_dir)

    outtmpl = str(work_dir / "%(title).80B [%(id)s].%(ext)s")

    def _progress_hook(d: dict[str, Any]) -> None:
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(
                strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
            )
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        if (
            (isinstance(total, (int, float)) and total > max_file_bytes)
            or (isinstance(downloaded, (int, float)) and downloaded > max_file_bytes)
        ):
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            )

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
        "progress_hooks": [_progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            if abort_event is not None and abort_event.is_set():
                raise DownloadError(
                    strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                )

            # Probe metadata first so live streams abort before any media bytes land.
            extracted = ydl.extract_info(url, download=False)
            if not isinstance(extracted, dict):
                raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)

            info = _pick_info(extracted)

            if abort_event is not None and abort_event.is_set():
                raise DownloadError(
                    strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
                )

            processed = ydl.process_ie_result(extracted, download=True)
            if isinstance(processed, dict):
                info = _pick_info(processed)

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

            if abort_event is not None and abort_event.is_set():
                raise DownloadError(
                    strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
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
    if not is_safe_media_url(url):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    abort_event = threading.Event()
    work_dir_holder: list[Path] = []
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _download_sync,
                url,
                download_dir,
                max_file_bytes,
                timeout_seconds,
                abort_event,
                work_dir_holder,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        abort_event.set()
        if work_dir_holder:
            _cleanup_dir(work_dir_holder[0])
        raise DownloadError(
            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
        ) from exc
    except asyncio.CancelledError:
        abort_event.set()
        if work_dir_holder:
            _cleanup_dir(work_dir_holder[0])
        raise


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
) -> DirectMediaStream | None:
    if not is_safe_media_url(url):
        return None

    ydl_opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            extracted = ydl.extract_info(url, download=False)
            if not isinstance(extracted, dict):
                return None
            info = _pick_info(extracted)

            title = str(info.get("title") or "")[:64]
            duration_raw = info.get("duration")
            duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None

            direct = info.get("url")
            if (
                isinstance(direct, str)
                and direct.startswith("http")
                and is_safe_media_url(direct)
                and ".m3u8" not in direct
                and ".mpd" not in direct
            ):
                ext = str(info.get("ext") or "mp4")
                kind = _classify_ext(ext)
                if kind is not MediaKind.DOCUMENT:
                    return DirectMediaStream(
                        direct_url=direct,
                        title=title,
                        kind=kind,
                        duration=duration,
                    )

            formats = info.get("formats") or []
            for f in reversed(formats):
                if not isinstance(f, dict):
                    continue
                u = f.get("url")
                if (
                    not isinstance(u, str)
                    or not u.startswith("http")
                    or not is_safe_media_url(u)
                ):
                    continue
                if ".m3u8" in u or ".mpd" in u:
                    continue
                proto = f.get("protocol")
                if proto not in ("http", "https"):
                    continue
                ext = str(f.get("ext") or "")
                kind = _classify_ext(ext)
                if kind is MediaKind.DOCUMENT:
                    continue
                if f.get("vcodec") == "none" or f.get("acodec") == "none":
                    continue
                size = f.get("filesize") or f.get("filesize_approx")
                if size and isinstance(size, (int, float)) and size > max_file_bytes:
                    continue
                return DirectMediaStream(
                    direct_url=u,
                    title=title,
                    kind=kind,
                    duration=duration,
                )
    except Exception as exc:
        logger.debug("Direct stream extraction skipped or failed for %s: %s", url, exc)
    return None


async def get_direct_stream(
    url: str,
    max_file_bytes: int,
    timeout_seconds: int = 15,
) -> DirectMediaStream | None:
    if not is_safe_media_url(url):
        return None

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_extract_direct_stream_sync, url, max_file_bytes),
            timeout=timeout_seconds,
        )
    except Exception:
        return None
