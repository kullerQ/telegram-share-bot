"""Public thumbnail URLs for platform logos in inline search results."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from telegram_share_bot.normalizer import normalize_url

# Unpadded public originals of the bundled Round Square PNGs. Pinning the
# commit keeps thumbnail availability testable while this branch is local.
_UPSTREAM_BASE_URL = (
    "https://raw.githubusercontent.com/YukiPixels/Pixel-Art-Icons/"
    "fb9c766d2aa3150b709add9da7126db8cb5afb28/224pxl/round%20square"
)
_UPSTREAM_FILENAME_EXCEPTIONS = {
    "bilibili.png": "bilibili28c.png",
    "twitch.png": "twitch-224z.png",
}

_LOGO_BY_HOST = {
    "artstation.com": "artstation.png",
    "b23.tv": "bilibili.png",
    "bilibili.com": "bilibili.png",
    "bsky.app": "bluesky.png",
    "chzzk.naver.com": "chzzk.png",
    "deviantart.com": "deviantart.png",
    "discord.com": "discord.png",
    "discord.gg": "discord.png",
    "facebook.com": "facebook.png",
    "fb.watch": "facebook.png",
    "github.com": "github.png",
    "instagram.com": "instagram.png",
    "itch.io": "itch.png",
    "kick.com": "kick.png",
    "ko-fi.com": "kofi.png",
    "patreon.com": "patreon.png",
    "pin.it": "pinterest.png",
    "pinterest.com": "pinterest.png",
    "pixiv.net": "pixiv.png",
    "redd.it": "reddit.png",
    "v.redd.it": "reddit.png",
    "reddit.com": "reddit.png",
    "steamcommunity.com": "steam.png",
    "steampowered.com": "steam.png",
    "tiktok.com": "tiktok.png",
    "twitch.tv": "twitch.png",
    "twitter.com": "twitter.png",
    "vxtwitter.com": "twitter.png",
    "fxtwitter.com": "twitter.png",
    "fixupx.com": "twitter.png",
    "x.com": "twitter.png",
    "youtu.be": "youtube.png",
    "youtube.com": "youtube.png",
    "youtube-nocookie.com": "youtube.png",
}


def thumbnail_url(url: str, base_url: str | None) -> str | None:
    """Choose a static logo using only the URL host; never fetch remote metadata."""
    if not base_url:
        return None
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for domain, filename in _LOGO_BY_HOST.items():
        if host == domain or host.endswith(f".{domain}"):
            if base_url == "upstream":
                upstream_name = _UPSTREAM_FILENAME_EXCEPTIONS.get(
                    filename, f"{filename.removesuffix('.png')}224.png"
                )
                return f"{_UPSTREAM_BASE_URL}/{upstream_name}"
            return f"{base_url.rstrip('/')}/{filename}"
    return None


def video_thumbnail_url(url: str) -> str | None:
    """Derive a public YouTube preview without fetching video metadata."""
    normalized = urlsplit(normalize_url(url))
    if normalized.scheme != "https" or normalized.hostname != "www.youtube.com":
        return None
    if normalized.path != "/watch":
        return None
    video_ids = parse_qs(normalized.query).get("v", [])
    if len(video_ids) != 1:
        return None
    video_id = video_ids[0]
    if len(video_id) != 11 or not all(
        char.isascii() and (char.isalnum() or char in "_-") for char in video_id
    ):
        return None
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
