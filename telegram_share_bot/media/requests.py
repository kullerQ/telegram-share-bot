"""Parse URLs, captions, and optional clip ranges from user messages."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from telegram_share_bot.media.models import MediaRequest, TimeRange
from telegram_share_bot.platforms.urls import is_youtube_url

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

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
