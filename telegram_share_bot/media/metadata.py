"""yt-dlp metadata extraction cache and source-info validation."""

from __future__ import annotations

import copy
import threading
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

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
_MAX_WRAPPED_INFO_ENTRIES = 32
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
    """Select a single media result from yt-dlp metadata and reject playlists."""
    if not isinstance(info, dict):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)
    if "entries" not in info:
        result_type = info.get("_type")
        if isinstance(result_type, str) and result_type in {"playlist", "multi_video"}:
            raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
        picked = cast(dict[str, Any], info)
    else:
        raw_entries = info.get("entries")
        if raw_entries is None or isinstance(raw_entries, (str, bytes, dict)):
            raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)

        try:
            entries = iter(raw_entries)
        except TypeError as exc:
            raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED) from exc

        picked = None
        raw_count = 0
        for _ in range(_MAX_WRAPPED_INFO_ENTRIES):
            try:
                entry = next(entries)
            except StopIteration:
                break
            except Exception as exc:
                raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED) from exc
            raw_count += 1
            candidate = _usable_wrapped_entry(entry)
            if candidate is not None:
                if picked is not None:
                    raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)
                picked = candidate

        if raw_count == _MAX_WRAPPED_INFO_ENTRIES:
            try:
                next(entries)
            except StopIteration:
                pass
            except Exception as exc:
                raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED) from exc
            else:
                raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)

    if picked is None:
        raise DownloadError(strings.DOWNLOAD_PLAYLIST_UNSUPPORTED)

    if picked.get("is_live") or picked.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError(strings.DOWNLOAD_LIVE_UNSUPPORTED)
    return picked


def _usable_wrapped_entry(entry: Any) -> dict[str, Any] | None:
    """Return a structurally useful video entry, skipping empty placeholders."""
    if not isinstance(entry, dict) or not entry:
        return None
    entry_type = entry.get("_type")
    if "entries" in entry or (
        isinstance(entry_type, str) and entry_type in {"playlist", "multi_video"}
    ):
        return None
    has_identity = isinstance(entry.get("id"), (str, int)) and bool(entry.get("id"))
    has_url = isinstance(entry.get("url"), str) and bool(entry.get("url"))
    has_formats = isinstance(entry.get("formats"), list) and bool(entry.get("formats"))
    if not (has_identity or has_url or has_formats):
        return None
    return cast(dict[str, Any], entry)


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


def is_twitter_animation(info: dict[str, Any]) -> bool:
    """Identify X animated GIFs, which yt-dlp downloads as silent MP4 files."""
    extractor = info.get("extractor_key") or info.get("extractor")
    is_twitter = isinstance(extractor, str) and extractor.casefold().startswith("twitter")
    media_type = info.get("media_type") or info.get("type")
    if is_twitter and isinstance(media_type, str) and media_type.casefold() in {
        "gif",
        "animated_gif",
    }:
        return True

    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        candidates = list(thumbnails)
    elif isinstance(thumbnails, dict):
        candidates = [thumbnails]
    else:
        candidates = []
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str):
        candidates.append({"url": thumbnail})
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        thumbnail_url = candidate.get("url")
        if not isinstance(thumbnail_url, str):
            continue
        try:
            parsed = urlsplit(thumbnail_url)
        except ValueError:
            continue
        if (
            is_twitter
            and parsed.scheme == "https"
            and parsed.hostname == "pbs.twimg.com"
            and "/tweet_video_thumb/" in parsed.path.lower()
        ):
            return True
    return False
