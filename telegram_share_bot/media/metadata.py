"""yt-dlp metadata extraction cache and source-info validation."""

from __future__ import annotations

import copy
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.media.models import (
    DownloadError,
    MediaFormat,
    TimeRange,
    VideoUnavailableError,
)

if TYPE_CHECKING:
    from yt_dlp.extractor.common import _InfoDict

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
        _extract_info_cache[url] = (
            now + _EXTRACT_INFO_TTL_SECONDS,
            copy.deepcopy(extracted),
        )
        while len(_extract_info_cache) > _EXTRACT_INFO_CACHE_MAX:
            oldest_key = next(iter(_extract_info_cache))
            _extract_info_cache.pop(oldest_key, None)

    return extracted, False


def _pick_info(info: Any) -> dict[str, Any]:
    """Select the first playable entry and reject unsupported live sources."""
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


def ensure_video_available(info: dict[str, Any], media_format: MediaFormat) -> None:
    """Reject audio-only sources when the user requested video."""
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


def source_title_and_duration(
    info: dict[str, Any], time_range: TimeRange | None
) -> tuple[str, int | None]:
    """Return the clipped duration and short title used in send results."""
    duration_raw = info.get("duration")
    duration = (
        time_range.duration_seconds
        if time_range is not None
        else int(duration_raw)
        if isinstance(duration_raw, (int, float))
        else None
    )
    return str(info.get("title") or "Media")[:64], duration
