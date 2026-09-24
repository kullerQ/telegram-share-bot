"""Download media URLs with yt-dlp under size and time limits."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import logging
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import download_range_func

if TYPE_CHECKING:
    from yt_dlp.extractor.common import _InfoDict

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
    TELEGRAM_CAPTION_MAX_LENGTH,
    CaptionMode,
)
from telegram_share_bot.normalizer import is_youtube_url, safe_url_for_log

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".temp", ".aria2"}

# Shared address space (CGNAT / some VPN overlays) — not covered by is_private.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


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

    for network in _BLOCKED_NETWORKS:
        if ip in network:
            return False

    return True


def is_safe_media_url(url: str, *, https_only: bool = False) -> bool:
    """Validate http(s) URL is not internal/private/loopback/cloud-metadata."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if https_only:
            if scheme != "https":
                return False
        elif scheme not in ("http", "https"):
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
        logger.warning(
            "URL security check rejected %s: %s", safe_url_for_log(url), exc
        )
        return False


def is_allowed_media_host(
    url: str, allowed_hosts: frozenset[str] | None
) -> bool:
    """Return True if the URL hostname matches the configured host allowlist.

    ``allowed_hosts is None`` means any host is permitted.
    Matching is suffix-based (``video.tiktok.com`` matches ``tiktok.com``).
    """
    if allowed_hosts is None:
        return True
    try:
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        host = hostname.strip().lower().rstrip(".").removeprefix("www.")
        if not host:
            return False
        for allowed in allowed_hosts:
            if host == allowed or host.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False


# True libc/resolver getaddrinfo — captured once so concurrent download threads
# never nest or uninstall each other's wrappers.
_REAL_GETADDRINFO = socket.getaddrinfo
_dns_guard_tls = threading.local()
_dns_guard_install_lock = threading.Lock()
_dns_guard_installed = False


def _dns_host_key(host: str | bytes | None) -> str | None:
    if host is None:
        return None
    text: str
    if isinstance(host, bytes):
        try:
            text = host.decode("idna")
        except UnicodeError:
            text = host.decode("utf-8", errors="replace")
    else:
        text = host
    cleaned = text.strip().lower().rstrip(".")
    return cleaned or None


def _guarded_getaddrinfo(
    host: str | bytes | None,
    port: str | bytes | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[Any, ...]]:
    state: dict[str, Any] | None = getattr(_dns_guard_tls, "state", None)
    if state is None:
        return _REAL_GETADDRINFO(host, port, family, type, proto, flags)

    key = _dns_host_key(host)
    results = _REAL_GETADDRINFO(host, port, family, type, proto, flags)
    safe_results: list[tuple[Any, ...]] = []
    for res in results:
        sockaddr = res[4]
        if not isinstance(sockaddr, Sequence) or not sockaddr:
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_safe_ip(ip):
            raise OSError(f"Blocked unsafe address for host {host!r}: {ip}")
        safe_results.append(res)

    if not safe_results:
        raise OSError(f"No safe addresses for host {host!r}")

    if key is None:
        return safe_results

    pinned_ips: dict[str, str] = state["pinned_ips"]
    pinned = pinned_ips.get(key)
    if pinned is not None:
        pinned_results = [
            res
            for res in safe_results
            if str(ipaddress.ip_address(res[4][0])) == pinned
        ]
        if pinned_results:
            return pinned_results
        # CDN / anycast hosts rotate A/AAAA sets within a single download.
        # Re-pin to a newly observed safe address instead of failing the request.

    pinned_ips[key] = str(ipaddress.ip_address(safe_results[0][4][0]))
    return [safe_results[0]]


def _ensure_dns_guard_installed() -> None:
    global _dns_guard_installed
    if _dns_guard_installed:
        return
    with _dns_guard_install_lock:
        if _dns_guard_installed:
            return
        socket.getaddrinfo = _guarded_getaddrinfo
        _dns_guard_installed = True


@contextlib.contextmanager
def _safe_dns_resolution() -> Generator[None, None, None]:
    """Re-validate DNS lookups and prefer a stable safe IP per host.

    Blocks private/CGNAT addresses on every lookup. Prefers the first
    validated IP for subsequent lookups in the same download context; if a
    CDN rotates that address out of the answer set, re-pins to a new safe IP
    rather than aborting (identity pins break TikTok/Akamai and similar CDNs).

    The getaddrinfo wrapper is installed once process-wide; per-download pin
    state lives in thread-local storage so concurrent ``asyncio.to_thread``
    downloads neither nest wrappers nor leak pins across hosts.
    """
    _ensure_dns_guard_installed()
    existing: dict[str, Any] | None = getattr(_dns_guard_tls, "state", None)
    if existing is not None:
        existing["depth"] = int(existing["depth"]) + 1
        try:
            yield
        finally:
            existing["depth"] = int(existing["depth"]) - 1
        return

    _dns_guard_tls.state = {"pinned_ips": {}, "depth": 1}
    try:
        yield
    finally:
        _dns_guard_tls.state = None


# In-process extract_info memoization: covers the get_direct_stream →
# download_media fallback so a cache miss costs one extraction, not two.
# Process-local only; short TTL; hard size bound; deep-copied on store/load
# because process_ie_result mutates the info dict.
_EXTRACT_INFO_TTL_SECONDS = 120
_EXTRACT_INFO_CACHE_MAX = 64
_extract_info_cache: dict[str, tuple[float, _InfoDict]] = {}
_extract_info_cache_lock = threading.Lock()

# CDN signed-URL / auth failures that mean a cached extract is stale.
_STALE_CDN_MARKERS = (
    "http error 403",
    "403: forbidden",
    "status code 403",
    "access denied",
    "forbidden",
    "url has expired",
    "expiredtoken",
    "signaturemismatch",
    "the downloaded file is empty",
)


def clear_extract_info_cache() -> None:
    """Test helper: drop memoized extract_info results."""
    with _extract_info_cache_lock:
        _extract_info_cache.clear()


def _evict_extract_info(url: str) -> None:
    with _extract_info_cache_lock:
        _extract_info_cache.pop(url, None)


def _looks_like_stale_cdn_url(exc: BaseException) -> bool:
    spaced = str(exc).lower()
    if any(marker in spaced for marker in _STALE_CDN_MARKERS):
        return True
    compact = spaced.replace(" ", "")
    # Compact form catches "HTTPError 403" / "ERROR: Unable to download ... 403"
    return "403" in compact and (
        "forbid" in compact or "denied" in compact or "http" in compact
    )


def _extract_info_cached(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    *,
    force_refresh: bool = False,
) -> tuple[_InfoDict, bool]:
    """Return ``(info_dict, from_cache)`` for ``url``.

    Cache hits return a deep copy so callers (``process_ie_result``) can mutate
    freely. Misses / forced refreshes call ``ydl.extract_info`` once and store a
    deep copy; the returned object is the fresh extract (safe to mutate).
    """
    now = time.monotonic()
    if not force_refresh:
        with _extract_info_cache_lock:
            hit = _extract_info_cache.get(url)
            if hit is not None:
                expires_at, cached = hit
                if now < expires_at:
                    return copy.deepcopy(cached), True
                _extract_info_cache.pop(url, None)

    extracted = ydl.extract_info(url, download=False)
    if not isinstance(extracted, dict):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)

    with _extract_info_cache_lock:
        _extract_info_cache[url] = (now + _EXTRACT_INFO_TTL_SECONDS, copy.deepcopy(extracted))
        while len(_extract_info_cache) > _EXTRACT_INFO_CACHE_MAX:
            oldest_key = next(iter(_extract_info_cache))
            _extract_info_cache.pop(oldest_key, None)

    return extracted, False


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


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Inclusive start / exclusive-feeling end in whole seconds (end > start)."""

    start: int
    end: int

    @property
    def duration_seconds(self) -> int:
        return self.end - self.start

    def cache_suffix(self) -> str:
        return f"#t={self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """Parsed inline/direct query: URL, optional caption, optional YouTube clip."""

    url: str | None
    custom_caption: str | None = None
    time_range: TimeRange | None = None


class DownloadError(Exception):
    """Raised when a URL cannot be downloaded within bot limits."""


# YouTube clip length hard cap (still offer both choices; clip path rejects over-long).
MAX_CLIP_SECONDS = 600

# Whole-token time range: start-end with seconds or h:mm:ss / m:ss forms.
_TIME_PART_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+)$")
_RANGE_TOKEN_RE = re.compile(r"^(.+)-(.+)$")


def _parse_time_part(part: str) -> int | None:
    """Parse ``SS``, ``M:SS``, or ``H:MM:SS`` into total seconds.

    ``M:SS`` allows minutes greater than 59 (e.g. ``90:12``).
    ``H:MM:SS`` requires minutes and seconds in 0-59.
    """
    match = _TIME_PART_RE.match(part)
    if match is None:
        return None
    left, mid, right = match.group(1), match.group(2), match.group(3)
    seconds = int(right)
    if left is not None and mid is not None:
        # H:MM:SS
        hours = int(left)
        minutes = int(mid)
        if minutes > 59 or seconds > 59:
            return None
        return hours * 3600 + minutes * 60 + seconds
    if left is not None:
        # M:SS (minutes may exceed 59)
        minutes = int(left)
        if seconds > 59:
            return None
        return minutes * 60 + seconds
    # Plain seconds
    return seconds


def parse_time_range_token(token: str) -> TimeRange | None:
    """Parse a single token as ``start-end``. Returns None if it is not a range."""
    match = _RANGE_TOKEN_RE.match(token.strip())
    if match is None:
        return None
    start = _parse_time_part(match.group(1))
    end = _parse_time_part(match.group(2))
    if start is None or end is None:
        return None
    if end <= start:
        return None
    return TimeRange(start=start, end=end)


def format_time_range(time_range: TimeRange) -> str:
    """Human-readable range for buttons and titles."""

    def _fmt(total: int) -> str:
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    return f"{_fmt(time_range.start)}-{_fmt(time_range.end)}"


def extract_url(text: str) -> str | None:
    url, _caption = extract_url_and_caption(text)
    return url


def extract_url_and_caption(text: str) -> tuple[str | None, str | None]:
    """Extract the first http(s) URL and optional caption text after it.

    Caption is everything after the matched URL (typically split by space),
    stripped. Trailing URL punctuation is not treated as part of the caption.
    """
    stripped = text.strip()
    match = URL_RE.search(stripped)
    if match is None:
        return None, None
    url = match.group(0).rstrip(").,]}>'\"")
    caption_raw = stripped[match.end() :].strip()
    return url, caption_raw or None


def extract_media_request(text: str) -> MediaRequest:
    """Extract URL, optional YouTube time range (first token only), and caption.

    A leading ``start-end`` token is treated as a clip only for YouTube URLs.
    On other hosts the same token stays part of the caption (or is ignored when
    captions are off). A YouTube link with caption text that is not a range
    keeps today's whole-video + caption behavior.
    """
    url, caption_raw = extract_url_and_caption(text)
    if url is None:
        return MediaRequest(url=None)
    if not caption_raw or not is_youtube_url(url):
        return MediaRequest(url=url, custom_caption=caption_raw)

    parts = caption_raw.split(None, 1)
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else None
    time_range = parse_time_range_token(first)
    if time_range is None:
        return MediaRequest(url=url, custom_caption=caption_raw)
    return MediaRequest(url=url, custom_caption=rest, time_range=time_range)


def sanitize_caption(text: str, *, max_length: int = 1024) -> str | None:
    """Strip control chars (except newline/tab) and enforce Telegram length."""
    cleaned = "".join(
        ch for ch in text if ch in "\n\t" or ord(ch) >= 32
    ).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def resolve_caption(
    mode: CaptionMode,
    *,
    media_title: str,
    custom_caption: str | None,
    max_length: int = TELEGRAM_CAPTION_MAX_LENGTH,
) -> str | None:
    """Pick the user-facing caption for the configured mode.

    Custom captions are never taken from cached media titles. Empty results
    become ``None`` (no caption). Callers must not enable ParseMode on captions.
    """
    if mode is CaptionMode.OFF:
        return None
    if mode is CaptionMode.CUSTOM:
        if custom_caption is None:
            return None
        return sanitize_caption(custom_caption, max_length=max_length)
    return sanitize_caption(media_title, max_length=max_length)


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


def is_https_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() == "https"


def _clamp_time_range(time_range: TimeRange, info: dict[str, Any]) -> TimeRange:
    """Reject ranges past video end; clamp end to duration when known."""
    duration_raw = info.get("duration")
    if not isinstance(duration_raw, (int, float)) or duration_raw <= 0:
        return time_range
    duration = int(duration_raw)
    if time_range.start >= duration:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    end = min(time_range.end, duration)
    if end <= time_range.start:
        raise DownloadError(strings.DOWNLOAD_CLIP_OUT_OF_BOUNDS)
    return TimeRange(start=time_range.start, end=end)


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
) -> DownloadedMedia:
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
        raise DownloadError(strings.DOWNLOAD_UNSAFE_URL)

    if time_range is not None:
        if time_range.duration_seconds > MAX_CLIP_SECONDS:
            raise DownloadError(
                strings.DOWNLOAD_CLIP_TOO_LONG.format(
                    max_minutes=MAX_CLIP_SECONDS // 60
                )
            )
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
                max_file_bytes=max_file_bytes,
                timeout_seconds=timeout_seconds,
                slide_ms=slideshow_slide_ms,
                max_images=slideshow_max_images,
                images_loop=slideshow_images_loop,
                abort_event=abort_event,
                https_only=https_only,
                allowed_hosts=allowed_hosts,
            )
            if slideshow is not None:
                return slideshow
        except DownloadError:
            _cleanup_dir(work_dir)
            raise
        except Exception as exc:
            _cleanup_dir(work_dir)
            logger.warning(
                "Slideshow path failed for %s: %s", safe_url_for_log(url), exc
            )
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    outtmpl = str(work_dir / "%(title).80B [%(id)s].%(ext)s")
    is_clip = time_range is not None

    def _progress_hook(d: dict[str, Any]) -> None:
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(
                strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
            )
        downloaded = d.get("downloaded_bytes") or 0
        if isinstance(downloaded, (int, float)) and downloaded > max_file_bytes:
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            )
        if is_clip:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if isinstance(total, (int, float)) and total > max_file_bytes:
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            )

    # Size-bounded formats for full downloads; height-capped for clips (full
    # filesize filters would reject short slices of long videos).
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
        "retries": 2,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook],
    }
    if is_clip:
        ydl_opts["format"] = "bv*[height<=1080]+ba/bv*[height<=720]+ba/b"
    else:
        ydl_opts["max_filesize"] = max_file_bytes
        ydl_opts["format"] = (
            f"bv*[filesize<{max_file_bytes}]+ba/"
            f"b[filesize<{max_file_bytes}]/"
            f"bv*[filesize_approx<{max_file_bytes}]+ba/"
            f"b[filesize_approx<{max_file_bytes}]"
        )

    effective_range = time_range

    try:
        with _safe_dns_resolution():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(
                            timeout_seconds=timeout_seconds
                        )
                    )

                # Probe metadata first so live streams abort before any media bytes land.
                # Reuse extract_info from get_direct_stream when still warm.
                extracted, from_cache = _extract_info_cached(ydl, url)
                info = _pick_info(extracted)

                if effective_range is not None:
                    effective_range = _clamp_time_range(effective_range, info)
                    ydl.params["download_ranges"] = cast(
                        Any,
                        download_range_func(
                            [],
                            [(effective_range.start, effective_range.end)],
                        ),
                    )

                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(
                            timeout_seconds=timeout_seconds
                        )
                    )

                try:
                    processed = ydl.process_ie_result(extracted, download=True)
                except yt_dlp.utils.DownloadError as download_exc:
                    # Cached extracts carry time-/IP-bound CDN URLs; one fresh
                    # re-extract recovers from 403/expired signatures.
                    if not (from_cache and _looks_like_stale_cdn_url(download_exc)):
                        raise
                    logger.info(
                        "Cached extract stale for %s; re-extracting: %s",
                        safe_url_for_log(url),
                        str(download_exc).split("\n")[-1].strip(),
                    )
                    _evict_extract_info(url)
                    extracted, _ = _extract_info_cached(
                        ydl, url, force_refresh=True
                    )
                    info = _pick_info(extracted)
                    if effective_range is not None:
                        effective_range = _clamp_time_range(effective_range, info)
                        ydl.params["download_ranges"] = cast(
                            Any,
                            download_range_func(
                                [],
                                [(effective_range.start, effective_range.end)],
                            ),
                        )
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(
                            strings.DOWNLOAD_TIMED_OUT.format(
                                timeout_seconds=timeout_seconds
                            )
                        ) from download_exc
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
                duration: int | None
                if effective_range is not None:
                    duration = effective_range.duration_seconds
                else:
                    duration_raw = info.get("duration")
                    duration = (
                        int(duration_raw)
                        if isinstance(duration_raw, (int, float))
                        else None
                    )

                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(
                        strings.DOWNLOAD_TIMED_OUT.format(
                            timeout_seconds=timeout_seconds
                        )
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
        logger.warning(
            "yt-dlp download error for %s: %s", safe_url_for_log(url), message
        )
        if "File is larger than max-filesize" in message or "filesize" in message.lower():
            raise DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            ) from exc
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc
    except Exception as exc:
        _cleanup_dir(work_dir)
        logger.warning(
            "Download failed for %s: %s", safe_url_for_log(url), exc
        )
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc


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
    allowed_hosts: frozenset[str] | None = None,
    *,
    https_only: bool = False,
    slideshow_slide_ms: int = 2500,
    slideshow_max_images: int = 35,
    slideshow_images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    time_range: TimeRange | None = None,
) -> DownloadedMedia:
    if not is_allowed_media_host(url, allowed_hosts):
        raise DownloadError(strings.DOWNLOAD_HOST_NOT_ALLOWED)
    if https_only and not is_https_url(url):
        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
    if not is_safe_media_url(url, https_only=https_only):
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
                https_only=https_only,
                allowed_hosts=allowed_hosts,
                slideshow_slide_ms=slideshow_slide_ms,
                slideshow_max_images=slideshow_max_images,
                slideshow_images_loop=slideshow_images_loop,
                time_range=time_range,
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
    *,
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
                duration = (
                    int(duration_raw)
                    if isinstance(duration_raw, (int, float))
                    else None
                )

                direct = info.get("url")
                if (
                    isinstance(direct, str)
                    and direct.startswith("http")
                    and is_safe_media_url(direct, https_only=https_only)
                    and ".m3u8" not in direct
                    and ".mpd" not in direct
                    and info.get("vcodec") != "none"
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
                https_only=https_only,
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return None
