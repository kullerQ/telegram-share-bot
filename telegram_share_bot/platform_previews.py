"""Bounded public-thumbnail lookups for Telegram inline choices."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from telegram_share_bot.normalizer import normalize_url, safe_url_for_log
from telegram_share_bot.platform_icons import video_thumbnail_url

logger = logging.getLogger(__name__)

_LOOKUP_TIMEOUT_SECONDS = 1.5
_MAX_HTML_BYTES = 96 * 1024
_MAX_JSON_BYTES = 256 * 1024
_CACHE_TTL_SECONDS = 300
_NEGATIVE_CACHE_TTL_SECONDS = 60
_CACHE_LIMIT = 128
_PREVIEW_CACHE: dict[str, tuple[float, Preview | None]] = {}
_REDDIT_POST_RE = re.compile(r"^/r/[^/]+/comments/([a-zA-Z0-9]+)(?:/|$)")
_X_STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)(?:/|$)")


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
    response = await client.get("https://www.tiktok.com/oembed", params={"url": url})
    response.raise_for_status()
    data = response.json()
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


async def resolve_preview(url: str) -> Preview | None:
    """Find a video thumbnail within a short time limit; return None for logo fallback."""
    youtube = video_thumbnail_url(url)
    if youtube is not None:
        return Preview(youtube, 320, 180)

    platform = _platform(url)
    if platform is None:
        return None
    normalized = normalize_url(url)
    cached = _PREVIEW_CACHE.get(normalized)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]

    # X redirects /video/1 links and can redirect lowercased account names.
    # Request the canonical post path with the original account name casing.
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
    started = time.monotonic()
    preview: Preview | None = None
    try:
        async with asyncio.timeout(_LOOKUP_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=_LOOKUP_TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramShareBot/1.0)"},
            ) as client:
                if platform == "tiktok":
                    preview = await _lookup_tiktok(target, client)
                elif platform == "reddit":
                    preview = await _lookup_reddit(target, client)
                else:
                    preview = await _lookup_page_image(target, platform, client)
    except (
        TimeoutError,
        httpx.HTTPError,
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
    elapsed = time.monotonic() - started
    logger.info(
        "Inline preview lookup for %s (%s): %s in %.2fs",
        safe_url_for_log(url),
        platform,
        "preview" if preview else "logo",
        elapsed,
    )
    ttl = _CACHE_TTL_SECONDS if preview else _NEGATIVE_CACHE_TTL_SECONDS
    _PREVIEW_CACHE[normalized] = (now + ttl, preview)
    while len(_PREVIEW_CACHE) > _CACHE_LIMIT:
        _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
    return preview
