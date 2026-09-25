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
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlsplit

import yt_dlp
from yt_dlp.utils import download_range_func

if TYPE_CHECKING:
    from yt_dlp.extractor.common import _InfoDict

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
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


def _is_transient_download_error(message: str) -> bool:
    """Recognize network failures that may succeed when the user retries."""
    message = message.lower()
    markers = (
        "timed out",
        "timeout",
        "temporary failure",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "connection aborted",
        "remote end closed",
        "network is unreachable",
        "http error 408",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
    )
    return any(marker in message for marker in markers)


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


class MediaFormat(str, Enum):
    """The requested output form, independent of the resulting file type."""

    VIDEO = "video"
    AUDIO = "audio"


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
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class _FormatCandidate:
    selector: str
    quality: tuple[float, float, float]
    estimated_size: int | None


_SOURCE_SIZE_MULTIPLIER = 2
_MAX_FORMAT_ATTEMPTS = 4


def _format_bytes(format_info: dict[str, Any], duration_scale: float) -> int | None:
    for key in ("filesize", "filesize_approx"):
        size = format_info.get(key)
        if isinstance(size, (int, float)) and size > 0:
            return max(1, int(size * duration_scale))
    bitrate = format_info.get("tbr") or format_info.get("abr")
    duration = format_info.get("duration")
    if (
        isinstance(bitrate, (int, float))
        and bitrate > 0
        and isinstance(duration, (int, float))
        and duration > 0
    ):
        return max(1, int(float(bitrate) * 1000 * float(duration) * duration_scale / 8))
    return None


def _format_candidates(
    info: dict[str, Any],
    *,
    max_file_bytes: int,
    duration_scale: float = 1.0,
) -> list[_FormatCandidate]:
    """Rank video choices by quality, preferring choices that may fit."""
    formats = info.get("formats")
    if not isinstance(formats, list):
        return []
    usable = [item for item in formats if isinstance(item, dict)]

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
        estimated = _format_bytes(item, duration_scale)
        if not has_audio:
            if best_audio is None:
                continue
            audio_id = format_id(best_audio)
            if audio_id is None:
                continue
            selector = f"{identifier}+{audio_id}"
            audio_size = _format_bytes(best_audio, duration_scale)
            if estimated is not None and audio_size is not None:
                estimated += audio_size
            elif estimated is not None:
                estimated = None
        if estimated is not None and estimated > max_file_bytes * _SOURCE_SIZE_MULTIPLIER:
            continue
        height = item.get("height")
        fps = item.get("fps")
        bitrate = item.get("tbr") or item.get("vbr")
        quality = (
            float(height) if isinstance(height, (int, float)) else 0.0,
            float(fps) if isinstance(fps, (int, float)) else 0.0,
            float(bitrate) if isinstance(bitrate, (int, float)) else 0.0,
        )
        candidates[selector] = _FormatCandidate(selector, quality, estimated)

    return sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.estimated_size is not None and candidate.estimated_size > max_file_bytes,
            -candidate.quality[0],
            -candidate.quality[1],
            -candidate.quality[2],
        ),
    )[:_MAX_FORMAT_ATTEMPTS]


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
        estimated = _format_bytes(item, duration_scale)
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
            candidate.estimated_size is not None
            and candidate.estimated_size > max_file_bytes,
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


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Clip window in whole seconds.

    ``end is None`` means through the end of the video (resolved at download).
    When ``end`` is set, it must be greater than ``start``.
    """

    start: int
    end: int | None = None

    @property
    def duration_seconds(self) -> int | None:
        if self.end is None:
            return None
        return self.end - self.start

    def cache_suffix(self) -> str:
        if self.end is None:
            return f"#t={self.start}-end"
        return f"#t={self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """Parsed inline/direct query: URL, optional caption, optional YouTube clip."""

    url: str | None
    custom_caption: str | None = None
    time_range: TimeRange | None = None


class DownloadError(Exception):
    """Raised when a URL cannot be downloaded within bot limits."""

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.retryable = (
            _is_transient_download_error(message)
            if retryable is None
            else retryable
        )


# YouTube clip length hard cap (still offer both choices; clip path rejects over-long).
MAX_CLIP_SECONDS = 600

# Whole-token time range: start-end with seconds or h:mm:ss / m:ss forms.
_TIME_PART_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+)$")
_RANGE_TOKEN_RE = re.compile(r"^(.+)-(.+)$")
_DURATION_SECONDS_RE = re.compile(r"^\d+$")
# YouTube share clock: ``1h2m3s``, ``33m42s``, ``90s`` (any non-empty combo).
_YT_CLOCK_RE = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$",
    re.IGNORECASE,
)


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


def _parse_youtube_timestamp_value(raw: str) -> int | None:
    """Parse a YouTube ``t`` / ``start`` value into whole seconds."""
    value = raw.strip()
    if not value:
        return None
    if _DURATION_SECONDS_RE.match(value):
        seconds = int(value)
        return seconds if seconds >= 0 else None
    match = _YT_CLOCK_RE.match(value)
    if match is None or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    secs = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + secs


def parse_youtube_start_seconds(url: str) -> int | None:
    """Return start offset from YouTube ``t`` / ``start`` query or ``#t=`` fragment."""
    try:
        parsed = urlsplit(url.strip())
    except Exception:
        return None

    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower() in ("t", "start"):
            start = _parse_youtube_timestamp_value(value)
            if start is not None:
                return start

    fragment = parsed.fragment or ""
    if fragment.lower().startswith("t="):
        start = _parse_youtube_timestamp_value(fragment[2:])
        if start is not None:
            return start
    # Rare: fragment is bare ``t=…`` already handled; also ``t=90s`` as sole fragment.
    if fragment:
        # ``#t=1h2m3s`` already covered; ``#90`` is not a YouTube convention.
        for key, value in parse_qsl(fragment, keep_blank_values=False):
            if key.lower() in ("t", "start"):
                start = _parse_youtube_timestamp_value(value)
                if start is not None:
                    return start
    return None


def parse_duration_seconds_token(token: str) -> int | None:
    """Parse a whole token as a positive duration in whole seconds."""
    if not _DURATION_SECONDS_RE.match(token.strip()):
        return None
    seconds = int(token.strip())
    return seconds if seconds >= 1 else None


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

    if time_range.end is None:
        return f"{_fmt(time_range.start)}-end"
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

    Clip recognition (YouTube only):

    - Leading ``start-end`` absolute range (e.g. ``1:20-2:05``)
    - ``t=`` / ``start=`` on the URL plus a positive seconds duration token
      (e.g. ``?t=2022`` + ``30`` -> clip 2022-2052)
    - ``t=`` / ``start=`` alone (or with a non-range/non-duration caption) ->
      open-ended clip from that start through the video end

    On other hosts trailing tokens stay part of the caption. Absolute ranges
    and duration tokens take precedence over open-ended ``t=``.
    """
    url, caption_raw = extract_url_and_caption(text)
    if url is None:
        return MediaRequest(url=None)
    if not is_youtube_url(url):
        return MediaRequest(url=url, custom_caption=caption_raw)

    url_start = parse_youtube_start_seconds(url)

    if caption_raw:
        parts = caption_raw.split(None, 1)
        first = parts[0]
        rest = parts[1] if len(parts) > 1 else None

        time_range = parse_time_range_token(first)
        if time_range is not None:
            return MediaRequest(url=url, custom_caption=rest, time_range=time_range)

        duration = parse_duration_seconds_token(first)
        if url_start is not None and duration is not None:
            return MediaRequest(
                url=url,
                custom_caption=rest,
                time_range=TimeRange(start=url_start, end=url_start + duration),
            )

    if url_start is not None:
        return MediaRequest(
            url=url,
            custom_caption=caption_raw,
            time_range=TimeRange(start=url_start, end=None),
        )

    return MediaRequest(url=url, custom_caption=caption_raw)


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


def ensure_full_media_duration(
    duration: object, max_duration_seconds: int
) -> None:
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
    raise DownloadError(
        strings.DOWNLOAD_MEDIA_TOO_LONG.format(duration_limit=duration_limit)
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


def _optimize_video_file(
    path: Path,
    *,
    max_file_bytes: int,
    deadline: float,
    abort_event: threading.Event | None,
    on_optimizing: Callable[[], None] | None,
) -> Path:
    """Re-encode an oversized video at most twice, keeping the smallest result."""
    if path.stat().st_size <= max_file_bytes:
        return path
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_MISSING)
    source_size = path.stat().st_size
    logger.info(
        "Optimizing video for Telegram: source_bytes=%d limit_bytes=%d",
        source_size,
        max_file_bytes,
    )
    if on_optimizing is not None:
        on_optimizing()

    best_path = path
    best_size = source_size
    attempts = ((28, "128k"), (33, "96k"))
    for index, (crf, audio_rate) in enumerate(attempts, start=1):
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0))
        output = path.parent / f"optimized_{index}.mp4"
        output.unlink(missing_ok=True)
        argv = [
            ffmpeg_bin,
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(best_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-sn",
            "-dn",
            "-vf",
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_rate,
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0)) from exc
        except OSError as exc:
            raise DownloadError(strings.DOWNLOAD_FIT_FFMPEG_FAILED) from exc
        if abort_event is not None and abort_event.is_set():
            output.unlink(missing_ok=True)
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        if completed.returncode != 0 or not output.exists():
            output.unlink(missing_ok=True)
            continue
        output_size = output.stat().st_size
        if output_size < best_size:
            if best_path != path:
                best_path.unlink(missing_ok=True)
            best_path = output
            best_size = output_size
            if best_size <= max_file_bytes:
                path.unlink(missing_ok=True)
                return best_path
        else:
            output.unlink(missing_ok=True)
    if best_size <= max_file_bytes:
        if best_path != path:
            path.unlink(missing_ok=True)
        return best_path
    if best_path != path:
        best_path.unlink(missing_ok=True)
    raise DownloadError(
        strings.DOWNLOAD_TOO_LARGE.format(
            size_mb=best_size // (1024 * 1024),
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
                ensure_full_media_duration(
                    slideshow.duration, max_media_duration_seconds
                )
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

    def _progress_hook(d: dict[str, Any]) -> None:
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
                if effective_range is None:
                    ensure_full_media_duration(
                        info.get("duration"), max_media_duration_seconds
                    )
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
                    )
                )
                selectors = [candidate.selector for candidate in candidates]
                fallback_selector = (
                    _default_audio_selector(effective_range, source_limit)
                    if media_format is MediaFormat.AUDIO
                    else _default_video_selector(effective_range, source_limit)
                )
                if fallback_selector not in selectors:
                    selectors.append(fallback_selector)
                if not selectors:
                    selectors = [fallback_selector]

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
                    transfer_started_at = time.monotonic()
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
                    optimized_path = _optimize_video_file(
                        best_oversized[0],
                        max_file_bytes=max_file_bytes,
                        deadline=deadline,
                        abort_event=abort_event,
                        on_optimizing=on_optimizing,
                    )
                    return DownloadedMedia(
                        path=optimized_path,
                        title=title,
                        kind=_classify(optimized_path),
                        duration=duration,
                    )
                if isinstance(last_error, DownloadError):
                    raise last_error
                if last_error is not None:
                    message = str(last_error).split("\n")[-1].strip()
                    if media_format is MediaFormat.AUDIO and _is_audio_unavailable_error(
                        message
                    ):
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
            max_media_duration_seconds=max_media_duration_seconds,
            on_optimizing=on_optimizing,
        )
    )
    try:
        completed, _ = await asyncio.wait({worker}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        abort_event.set()
        worker.add_done_callback(
            lambda task: _cleanup_finished_download(task, work_dir_holder)
        )
        raise

    if worker not in completed:
        abort_event.set()
        worker.add_done_callback(
            lambda task: _cleanup_finished_download(task, work_dir_holder)
        )
        raise DownloadError(
            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds)
        )
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
                duration = (
                    int(duration_raw)
                    if isinstance(duration_raw, (int, float))
                    else None
                )

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
