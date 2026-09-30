"""Bounded public-thumbnail lookups for Telegram inline choices."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from yt_dlp.jsinterp import js_number_to_string

from telegram_share_bot.media.models import DownloadError
from telegram_share_bot.media.network import GuardedAsyncHTTPTransport
from telegram_share_bot.media.work import MediaWorkSupervisor
from telegram_share_bot.platforms.icons import video_thumbnail_url
from telegram_share_bot.platforms.urls import normalize_url, safe_url_for_log
from telegram_share_bot.tiktok.source import TikTokPhotoRef, extract_slideshow

logger = logging.getLogger(__name__)

_LOOKUP_TIMEOUT_SECONDS = 1.5
_TIKTOK_LOOKUP_TIMEOUT_SECONDS = 3.5
_MAX_HTML_BYTES = 96 * 1024
_MAX_JSON_BYTES = 256 * 1024
_CACHE_TTL_SECONDS = 300
_NEGATIVE_CACHE_TTL_SECONDS = 60
_CACHE_LIMIT = 128
_MAX_ACTIVE_LOOKUPS = 4
_TIKTOK_PHOTO_WORKERS = 2
_REDDIT_POST_RE = re.compile(r"^/r/[^/]+/comments/([a-zA-Z0-9]+)(?:/|$)")
_X_STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)(?:/|$)")
_X_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_TIKTOK_MEDIA_RE = re.compile(r"^/@[^/]+/(video|photo)/(\d+)(?:/|$)")


@dataclass(frozen=True, slots=True)
class Preview:
    url: str
    width: int | None = None
    height: int | None = None


class _ImageMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.image_url is not None:
            return
        values = dict(attrs)
        key = values.get("property") or values.get("name")
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            self.image_url = values.get("content")




def _matches_host(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if _matches_host(host, "tiktok.com"):
        return "tiktok"
    if host in {"x.com", "twitter.com", "vxtwitter.com", "fxtwitter.com", "fixupx.com"}:
        return "x"
    if _matches_host(host, "instagram.com"):
        return "instagram"
    if host in {"redd.it", "v.redd.it"} or _matches_host(host, "reddit.com"):
        return "reddit"
    if host == "fb.watch" or _matches_host(host, "facebook.com"):
        return "facebook"
    return None


def _trusted_image_url(url: object, platform: str) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    cdns = {
        "tiktok": ("tiktokcdn.com", "tiktokcdn-eu.com", "muscdn.com", "tiktok.com"),
        "x": ("pbs.twimg.com", "video.twimg.com"),
        "instagram": ("cdninstagram.com", "fbcdn.net"),
        "reddit": ("preview.redd.it", "external-preview.redd.it", "i.redd.it", "redditmedia.com"),
        "facebook": ("fbcdn.net",),
    }
    if any(_matches_host(host, domain) for domain in cdns[platform]):
        return url
    return None


async def _lookup_tiktok(url: str, client: httpx.AsyncClient) -> Preview | None:
    body = bytearray()
    async with client.stream(
        "GET", "https://www.tiktok.com/oembed", params={"url": url}
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_JSON_BYTES:
                return None
            body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        return None
    thumbnail = _trusted_image_url(data.get("thumbnail_url"), "tiktok")
    if thumbnail is None:
        return None
    width = data.get("thumbnail_width")
    height = data.get("thumbnail_height")
    return Preview(
        thumbnail,
        width if isinstance(width, int) and width > 0 else None,
        height if isinstance(height, int) and height > 0 else None,
    )


async def _lookup_tiktok_media(
    url: str,
    client: httpx.AsyncClient,
    photo_work: MediaWorkSupervisor,
) -> Preview | None:
    parsed = urlsplit(url)
    match = _TIKTOK_MEDIA_RE.match(parsed.path)
    if match is None and parsed.hostname in {"vt.tiktok.com", "vm.tiktok.com"}:
        response = await client.get(url)
        if response.status_code not in {301, 302, 307, 308}:
            return None
        location = response.headers.get("location")
        if not location:
            return None
        resolved = urlsplit(urljoin(url, location))
        if resolved.scheme != "https" or resolved.hostname != "www.tiktok.com":
            return None
        match = _TIKTOK_MEDIA_RE.match(resolved.path)
        if match is None:
            return None
        url = urlunsplit(("https", "www.tiktok.com", resolved.path, "", ""))
        parsed = urlsplit(url)
    if match is not None and match.group(1) == "photo":
        ref = TikTokPhotoRef(
            user=parsed.path.split("/")[1],
            video_id=match.group(2),
            canonical_url=urlunsplit(("https", "www.tiktok.com", parsed.path, "", "")),
        )
        lease = await photo_work.try_acquire(_TIKTOK_LOOKUP_TIMEOUT_SECONDS)
        if lease is None:
            logger.debug("TikTok photo preview skipped because native preview capacity is full")
            return None
        try:
            source = await lease.run_sync(
                extract_slideshow,
                ref,
                max_images=1,
                https_only=True,
                socket_timeout=3,
                wait_timeout_seconds=min(2.75, lease.remaining_seconds()),
            )
        except (DownloadError, TimeoutError):
            return None
        finally:
            await lease.release()
        image_url = _trusted_image_url(next(iter(source.image_urls), None), "tiktok")
        return Preview(image_url) if image_url else None
    return await _lookup_tiktok(url, client)


async def _lookup_page_image(url: str, platform: str, client: httpx.AsyncClient) -> Preview | None:
    parser = _ImageMetaParser()
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", "").lower():
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk[: _MAX_HTML_BYTES - len(body)])
            if b"</head>" in body or len(body) >= _MAX_HTML_BYTES:
                break
        parser.feed(body.decode("utf-8", "ignore"))
    image_url = _trusted_image_url(parser.image_url, platform)
    if image_url is not None and platform == "x":
        parts = urlsplit(image_url)
        if parts.hostname == "pbs.twimg.com":
            image_url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode({"format": "jpg", "name": "small"}),
                    "",
                )
            )
    return Preview(image_url) if image_url else None


def _x_syndication_token(status_id: str) -> str:
    """Generate the short token used by X's public embedded-post endpoint."""
    value = (int(status_id) / 1e15) * math.pi
    return js_number_to_string(value, 36).translate(str.maketrans(dict.fromkeys("0.")))


def _x_preview_from_payload(data: object) -> Preview | None:
    if not isinstance(data, dict):
        return None
    posts = [data]
    quoted = data.get("quoted_tweet")
    if isinstance(quoted, dict):
        posts.append(quoted)

    for post in posts:
        media = post.get("mediaDetails")
        candidates = media if isinstance(media, list) else []
        photos = post.get("photos")
        if isinstance(photos, list):
            candidates = [*candidates, *photos]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            image_url = _trusted_image_url(
                item.get("media_url_https") or item.get("media_url") or item.get("url"),
                "x",
            )
            if image_url is None:
                continue
            parts = urlsplit(image_url)
            if parts.hostname == "pbs.twimg.com":
                image_url = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        parts.path,
                        urlencode({"format": "jpg", "name": "small"}),
                        "",
                    )
                )
            sizes = item.get("sizes")
            medium = sizes.get("medium") if isinstance(sizes, dict) else None
            width = medium.get("w") if isinstance(medium, dict) else None
            height = medium.get("h") if isinstance(medium, dict) else None
            return Preview(
                image_url,
                width if isinstance(width, int) and width > 0 else None,
                height if isinstance(height, int) and height > 0 else None,
            )
    return None


async def _lookup_x_syndication(url: str, client: httpx.AsyncClient) -> Preview | None:
    status = _X_STATUS_RE.match(urlsplit(url).path)
    if status is None:
        return None
    status_id = status.group(2)
    body = bytearray()
    try:
        async with client.stream(
            "GET",
            _X_SYNDICATION_URL,
            params={"id": status_id, "token": _x_syndication_token(status_id)},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_JSON_BYTES:
                    return None
                body.extend(chunk)
        return _x_preview_from_payload(json.loads(body))
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        return None


async def _lookup_reddit(url: str, client: httpx.AsyncClient) -> Preview | None:
    parsed = urlsplit(url)
    match = _REDDIT_POST_RE.match(parsed.path)
    post_id = match.group(1) if match else None
    if post_id is None and parsed.hostname == "redd.it":
        candidate = parsed.path.strip("/")
        post_id = candidate if candidate.isascii() and candidate.isalnum() else None
    if post_id is None:
        return None
    body = bytearray()
    async with client.stream(
        "GET",
        f"https://www.reddit.com/comments/{post_id}.json",
        params={"raw_json": "1", "limit": "0"},
        headers={"User-Agent": "telegram-share-bot/1.0 (public inline preview)"},
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_JSON_BYTES:
                return None
            body.extend(chunk)
    listing = json.loads(body)
    post = listing[0]["data"]["children"][0]["data"]
    if not isinstance(post, dict) or not post.get("is_video"):
        return None
    images = post.get("preview", {}).get("images", [])
    source = images[0].get("source", {}) if images else {}
    image_url = _trusted_image_url(source.get("url"), "reddit")
    if image_url is None:
        image_url = _trusted_image_url(post.get("thumbnail"), "reddit")
    if image_url is None:
        return None
    width, height = source.get("width"), source.get("height")
    return Preview(
        image_url,
        width if isinstance(width, int) and width > 0 else None,
        height if isinstance(height, int) and height > 0 else None,
    )


class PreviewResolver:
    """Own bounded HTTP preview lookups, cache, and native TikTok work."""

    def __init__(
        self,
        *,
        max_active_lookups: int = _MAX_ACTIVE_LOOKUPS,
        photo_workers: int = _TIKTOK_PHOTO_WORKERS,
    ) -> None:
        self._max_active_lookups = max(1, max_active_lookups)
        self._active_lookups = 0
        self._photo_work = MediaWorkSupervisor(max(1, photo_workers), max_waiters=0)
        self._cache: dict[str, tuple[float, Preview | None]] = {}
        self._inflight: dict[str, asyncio.Task[Preview | None]] = {}
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    async def start(self, client: httpx.AsyncClient | None = None) -> None:
        """Open the app-owned HTTP client, or accept a client in isolated tests."""
        if self._closed:
            raise RuntimeError("Preview resolver is closed")
        if self._client is not None:
            return
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(_TIKTOK_LOOKUP_TIMEOUT_SECONDS),
            limits=httpx.Limits(
                max_connections=_MAX_ACTIVE_LOOKUPS,
                max_keepalive_connections=_MAX_ACTIVE_LOOKUPS,
            ),
            follow_redirects=False,
            transport=GuardedAsyncHTTPTransport(),
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramShareBot/1.0)"},
        )

    async def close(self) -> None:
        """Stop shared lookups and close the application's connection pool."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def resolve(self, url: str) -> Preview | None:
        """Return a cached/derived preview, or quickly fall back when saturated."""
        youtube = video_thumbnail_url(url)
        if youtube is not None:
            return Preview(youtube, 320, 180)

        platform = _platform(url)
        client = self._client
        if platform is None or client is None or self._closed:
            return None
        normalized = normalize_url(url)
        now = time.monotonic()
        cached = self._cache.get(normalized)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            self._cache.pop(normalized, None)

        inflight = self._inflight.get(normalized)
        if inflight is not None:
            return await asyncio.shield(inflight)

        # The counter update and task registration happen before the first
        # suspension, so a burst of unique URLs cannot create an unbounded queue.
        if self._active_lookups >= self._max_active_lookups:
            logger.debug("Inline preview capacity is full; using platform logo")
            return None
        self._active_lookups += 1
        try:
            task = asyncio.create_task(
                self._resolve_owned(url, normalized, platform, client),
                name="inline-platform-preview",
            )
        except BaseException:
            self._active_lookups -= 1
            raise
        self._inflight[normalized] = task
        return await asyncio.shield(task)

    async def _resolve_owned(
        self,
        url: str,
        normalized: str,
        platform: str,
        client: httpx.AsyncClient,
    ) -> Preview | None:
        started = time.monotonic()
        preview: Preview | None = None
        lookup_timeout = (
            _TIKTOK_LOOKUP_TIMEOUT_SECONDS if platform == "tiktok" else _LOOKUP_TIMEOUT_SECONDS
        )
        # X /video/1 redirects and case-normalization can hide otherwise valid cards.
        if platform == "x":
            path = urlsplit(url).path
            status = _X_STATUS_RE.match(path)
            if status is not None:
                path = f"/{status.group(1)}/status/{status.group(2)}"
            target = urlunsplit(("https", "x.com", path, "", ""))
        elif platform == "instagram":
            target = normalized
        else:
            target = url

        try:
            async with asyncio.timeout(lookup_timeout):
                if platform == "tiktok":
                    preview = await _lookup_tiktok_media(target, client, self._photo_work)
                elif platform == "reddit":
                    preview = await _lookup_reddit(target, client)
                elif platform == "x":
                    preview = await _lookup_x_syndication(target, client)
                    if preview is None:
                        preview = await _lookup_page_image(target, platform, client)
                else:
                    preview = await _lookup_page_image(target, platform, client)
        except (
            TimeoutError,
            httpx.HTTPError,
            DownloadError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as exc:
            logger.debug(
                "Inline preview lookup failed for %s (%s): %s",
                safe_url_for_log(url),
                platform,
                type(exc).__name__,
            )
        else:
            elapsed = time.monotonic() - started
            logger.info(
                "Inline preview lookup for %s (%s): %s in %.2fs",
                safe_url_for_log(url),
                platform,
                "preview" if preview else "logo",
                elapsed,
            )
        finally:
            self._active_lookups -= 1
            task = asyncio.current_task()
            if task is not None and self._inflight.get(normalized) is task:
                self._inflight.pop(normalized, None)

        ttl = _CACHE_TTL_SECONDS if preview else _NEGATIVE_CACHE_TTL_SECONDS
        self._cache[normalized] = (time.monotonic() + ttl, preview)
        while len(self._cache) > _CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))
        return preview


async def resolve_preview(
    url: str,
    resolver: PreviewResolver | None = None,
) -> Preview | None:
    """Resolve with the application service; YouTube thumbnails need no network."""
    youtube = video_thumbnail_url(url)
    if youtube is not None:
        return Preview(youtube, 320, 180)
    if resolver is None:
        return None
    return await resolver.resolve(url)
