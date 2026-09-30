"""Extract safe direct media streams from supported URLs."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, cast

from telegram_share_bot import strings
from telegram_share_bot.media.files import _classify_ext
from telegram_share_bot.media.metadata import (
    _extract_info_cached,
    _pick_info,
    is_twitter_animation,
)
from telegram_share_bot.media.models import (
    DirectMediaStream,
    DownloadError,
    MediaFormat,
    MediaKind,
)
from telegram_share_bot.media.network import create_youtube_dl
from telegram_share_bot.media.security import (
    _safe_dns_resolution,
    is_allowed_media_host,
    is_https_url,
    is_safe_media_url,
)
from telegram_share_bot.media.work import MediaWorkLease
from telegram_share_bot.platforms.urls import safe_url_for_log

logger = logging.getLogger(__name__)


def _extract_direct_stream_sync(
    url: str,
    max_file_bytes: int,
    *,
    abort_event: threading.Event | None = None,
    allowed_hosts: frozenset[str] | None = None,
    media_format: MediaFormat = MediaFormat.VIDEO,
    https_only: bool = False,
) -> DirectMediaStream | None:
    if abort_event is not None and abort_event.is_set():
        return None
    if not is_allowed_media_host(url, allowed_hosts):
        return None
    if https_only and not is_https_url(url):
        return None
    if not is_safe_media_url(url, https_only=https_only):
        return None

    # Photo posts have no playable video stream — skip so callers fall back to
    # the slideshow compiler in download_media.
    from telegram_share_bot.tiktok.source import detect_tiktok_photo_post

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
            with create_youtube_dl(ydl_opts) as ydl:
                extracted, _from_cache = _extract_info_cached(ydl, url)
                if abort_event is not None and abort_event.is_set():
                    return None
                info = _pick_info(extracted)

                title = str(info.get("title") or "")[:64]
                duration_raw = info.get("duration")
                duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None

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
                    if media_format is MediaFormat.VIDEO and is_twitter_animation(info):
                        kind = MediaKind.ANIMATION
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
                    if media_format is MediaFormat.VIDEO and is_twitter_animation(info):
                        kind = MediaKind.ANIMATION
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
    work_lease: MediaWorkLease | None = None,
    on_worker_registered: Callable[[asyncio.Task[Any]], None] | None = None,
) -> DirectMediaStream | None:
    if not is_allowed_media_host(url, allowed_hosts):
        return None

    owns_lease = work_lease is None
    active_lease = work_lease or MediaWorkLease(
        None, time.monotonic() + max(0, timeout_seconds)
    )
    abort_event = threading.Event()
    try:
        remaining = min(float(timeout_seconds), active_lease.remaining_seconds())
        return cast(
            DirectMediaStream | None,
            await active_lease.run_sync(
                _extract_direct_stream_sync,
                url,
                max_file_bytes,
                wait_timeout_seconds=remaining,
                abort_event=abort_event,
                on_worker_registered=on_worker_registered,
                allowed_hosts=allowed_hosts,
                media_format=media_format,
                https_only=https_only,
            ),
        )
    except TimeoutError as exc:
        abort_event.set()
        raise DownloadError(
            strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=timeout_seconds),
            retryable=True,
        ) from exc
    except Exception:
        return None
    finally:
        if owns_lease:
            await active_lease.release()
