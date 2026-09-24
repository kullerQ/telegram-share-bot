"""URL normalization and canonicalization utilities."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking and ephemeral query parameters to strip across all platforms.
GENERIC_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "si",
    "fbclid",
    "gclid",
    "ref",
    "ref_src",
    "feature",
    "igsh",
    "source",
    "context",
    "sub_source",
}

# Query keys that typically indicate time-limited / credentialed URLs.
_SIGNED_QUERY_KEYS = frozenset(
    {
        "signature",
        "sig",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-algorithm",
        "awsaccesskeyid",
        "access_token",
        "auth",
        "token",
        "expires",
        "expire",
        "key-pair-id",
        "policy",
        "x-goog-signature",
        "x-goog-credential",
        "x-goog-algorithm",
    }
)

# YouTube-specific video ID pattern (11 characters: alphanumeric, dash, underscore).
_YT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
# Twitter / X status URL pattern.
_TWITTER_STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)")
# Instagram post/reel pattern.
_INSTAGRAM_POST_RE = re.compile(r"^/(reel|reels|p)/([a-zA-Z0-9_-]+)")
# TikTok video / photo patterns.
_TIKTOK_VIDEO_RE = re.compile(r"^/(@[^/]+)/video/(\d+)")
_TIKTOK_PHOTO_RE = re.compile(r"^/(@[^/]+)/photo/(\d+)")

# Domains whose normalized forms are public content ids (safe to share-cache).
_PUBLIC_CACHE_HOSTS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "tiktok.com",
    }
)

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
    }
)


def is_youtube_url(raw_url: str) -> bool:
    """Return True if the URL host is YouTube (including youtu.be / nocookie)."""
    clean = raw_url.strip()
    if not clean:
        return False
    try:
        host = (
            urlsplit(clean)
            .netloc.lower()
            .removeprefix("www.")
            .removeprefix("m.")
            .removeprefix("music.")
        )
    except Exception:
        return False
    return host in _YOUTUBE_HOSTS


def looks_signed_url(raw_url: str) -> bool:
    """Return True if the URL appears to carry auth / signed query parameters."""
    if not raw_url.strip():
        return False
    parsed = urlsplit(raw_url.strip())
    for key, _value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower() in _SIGNED_QUERY_KEYS:
            return True
    return False


def is_public_cacheable_url(raw_url: str) -> bool:
    """Whether a URL is safe to share across users in the file_id cache."""
    if looks_signed_url(raw_url):
        return False
    parsed = urlsplit(raw_url.strip())
    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("m.")
        .removeprefix("mobile.")
    )
    # Platform-normalized public posts are fine; generic hosts with leftover
    # query params may still be private — only cache known public platforms or
    # URLs with no query string.
    if host in _PUBLIC_CACHE_HOSTS or host.endswith(".tiktok.com"):
        return True
    return not parsed.query


def safe_url_for_log(raw_url: str) -> str:
    """Return a log/UI-safe URL without signed or arbitrary query secrets.

    Known public platforms keep their canonical form (e.g. YouTube ``?v=``).
    Signed / credentialed URLs and other query strings are stripped to path only.
    """
    clean = raw_url.strip()
    if not clean:
        return ""
    if looks_signed_url(clean):
        parsed = urlsplit(clean)
        if not parsed.scheme and not parsed.netloc:
            return clean[:120]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "", "", ""))

    normalized = normalize_url(clean) or clean
    parsed = urlsplit(normalized)
    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("m.")
        .removeprefix("mobile.")
    )
    if host in _PUBLIC_CACHE_HOSTS or host.endswith(".tiktok.com"):
        return normalized
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "", "", ""))


def normalize_url(raw_url: str) -> str:
    """Normalize a media URL into a canonical form for consistent cache keys.

    - Canonicalizes scheme and hostname (lowercase, strips 'www.').
    - Standardizes platform-specific video links (YouTube watch/shorts/youtu.be,
      Twitter/X, Instagram, TikTok).
    - Removes common analytics/tracking query parameters (e.g. `si`, `utm_*`).
    - Strips URL fragments and sorts remaining query parameters.
    """
    clean_url = raw_url.strip()
    if not clean_url:
        return ""

    parsed = urlsplit(clean_url)
    if not parsed.netloc and not parsed.path:
        return clean_url

    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()

    # Remove default ports
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    elif netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]

    path = parsed.path
    query_tuples = parse_qsl(parsed.query, keep_blank_values=False)

    # 1. YouTube normalization
    normalized_yt = _normalize_youtube(netloc, path, query_tuples)
    if normalized_yt is not None:
        return normalized_yt

    # 2. Twitter / X normalization
    normalized_twitter = _normalize_twitter(netloc, path)
    if normalized_twitter is not None:
        return normalized_twitter

    # 3. Instagram normalization
    normalized_ig = _normalize_instagram(netloc, path)
    if normalized_ig is not None:
        return normalized_ig

    # 4. TikTok normalization
    normalized_tiktok = _normalize_tiktok(netloc, path)
    if normalized_tiktok is not None:
        return normalized_tiktok

    # 5. Generic normalization for other domains
    domain = netloc.removeprefix("www.")
    clean_params = [
        (k, v) for k, v in query_tuples if k.lower() not in GENERIC_TRACKING_PARAMS
    ]
    clean_params.sort(key=lambda item: item[0])
    query_string = urlencode(clean_params)

    # Clean redundant trailing slashes on root
    if path == "/":
        path = ""
    elif path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    return urlunsplit((scheme, domain, path, query_string, ""))


def _normalize_youtube(
    netloc: str, path: str, query_tuples: list[tuple[str, str]]
) -> str | None:
    domain = netloc.removeprefix("www.").removeprefix("m.")
    video_id: str | None = None

    if domain == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
        if _YT_ID_RE.match(candidate):
            video_id = candidate
    elif domain in ("youtube.com", "youtube-nocookie.com"):
        if path.startswith("/shorts/"):
            candidate = path.removeprefix("/shorts/").split("/")[0]
            if _YT_ID_RE.match(candidate):
                video_id = candidate
        elif path.startswith("/embed/"):
            candidate = path.removeprefix("/embed/").split("/")[0]
            if _YT_ID_RE.match(candidate):
                video_id = candidate
        elif path in ("/watch", "/watch_popup"):
            for k, v in query_tuples:
                if k == "v" and _YT_ID_RE.match(v):
                    video_id = v
                    break

    if video_id is not None:
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def _normalize_twitter(netloc: str, path: str) -> str | None:
    domain = netloc.removeprefix("www.").removeprefix("mobile.")
    if domain in ("twitter.com", "x.com", "vxtwitter.com", "fxtwitter.com", "fixupx.com"):
        match = _TWITTER_STATUS_RE.match(path)
        if match:
            user, tweet_id = match.group(1), match.group(2)
            return f"https://x.com/{user.lower()}/status/{tweet_id}"
    return None


def _normalize_instagram(netloc: str, path: str) -> str | None:
    domain = netloc.removeprefix("www.")
    if domain == "instagram.com":
        match = _INSTAGRAM_POST_RE.match(path)
        if match:
            kind, code = match.group(1), match.group(2)
            std_kind = "reel" if kind.startswith("reel") else "p"
            return f"https://www.instagram.com/{std_kind}/{code}/"
    return None


def _normalize_tiktok(netloc: str, path: str) -> str | None:
    domain = netloc.removeprefix("www.")
    if domain == "tiktok.com":
        match = _TIKTOK_VIDEO_RE.match(path)
        if match:
            user, video_id = match.group(1), match.group(2)
            return f"https://www.tiktok.com/{user}/video/{video_id}"
        photo_match = _TIKTOK_PHOTO_RE.match(path)
        if photo_match:
            user, photo_id = photo_match.group(1), photo_match.group(2)
            return f"https://www.tiktok.com/{user}/photo/{photo_id}"
    return None
