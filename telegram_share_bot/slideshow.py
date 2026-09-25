"""TikTok photo-post (slideshow) detection, extraction, and ffmpeg muxing."""

from __future__ import annotations

import logging
import math
import re
import shutil
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.networking.impersonate import ImpersonateTarget

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_SLIDESHOW_IMAGES_LOOP,
    DEFAULT_SLIDESHOW_MAX_IMAGES,
    DEFAULT_SLIDESHOW_SLIDE_MS,
)
from telegram_share_bot.downloader import (
    DownloadedMedia,
    DownloadError,
    MediaKind,
    _safe_dns_resolution,
    is_safe_media_url,
)
from telegram_share_bot.normalizer import safe_url_for_log

logger = logging.getLogger(__name__)

_TIKTOK_PHOTO_RE = re.compile(r"^/(@[\w.\-]+)/photo/(\d+)")
_TIKTOK_SHORT_HOSTS = frozenset({"vt.tiktok.com", "vm.tiktok.com"})
_TIKTOK_HOST_SUFFIXES = frozenset({"tiktok.com"})

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


@dataclass(frozen=True, slots=True)
class TikTokPhotoRef:
    user: str
    video_id: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class SlideshowSource:
    image_urls: tuple[str, ...]
    audio_url: str | None
    title: str
    canonical_url: str
    audio_duration: float | None = None


# Memoize short-link resolutions so get_direct_stream + download_media share one lookup.
_SHORT_LINK_TTL_SECONDS = 300.0
_short_link_cache: dict[str, tuple[float, TikTokPhotoRef | None]] = {}
_short_link_lock = threading.Lock()


def _is_tiktok_host(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if not host:
        return False
    if host in _TIKTOK_SHORT_HOSTS:
        return True
    for suffix in _TIKTOK_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _photo_ref_from_url(url: str) -> TikTokPhotoRef | None:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    hostname = parsed.hostname
    if not hostname or not _is_tiktok_host(hostname):
        return None
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if host in _TIKTOK_SHORT_HOSTS:
        return None
    match = _TIKTOK_PHOTO_RE.match(parsed.path or "")
    if match is None:
        return None
    user, video_id = match.group(1), match.group(2)
    return TikTokPhotoRef(
        user=user,
        video_id=video_id,
        canonical_url=f"https://www.tiktok.com/{user}/photo/{video_id}",
    )


def _resolve_short_link(url: str) -> TikTokPhotoRef | None:
    """Follow a vt/vm.tiktok.com redirect and return a photo ref if applicable."""
    now = time.monotonic()
    with _short_link_lock:
        cached = _short_link_cache.get(url)
        if cached is not None:
            expires_at, ref = cached
            if now < expires_at:
                return ref
            _short_link_cache.pop(url, None)

    resolved: TikTokPhotoRef | None = None
    try:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            # Prefer HEAD; fall back to GET if the CDN rejects HEAD.
            final_url: str | None = None
            for method in ("HEAD", "GET"):
                try:
                    request = Request(
                        url,
                        method=method,
                        extensions={"impersonate": ImpersonateTarget("chrome")},
                    )
                    response = ydl.urlopen(request)  # type: ignore[arg-type]
                    final_url = getattr(response, "url", None) or url
                    # Drain/close to free the connection.
                    with response:
                        if method == "GET":
                            _ = response.read(64)
                    break
                except RequestError:
                    continue
            if final_url:
                resolved = _photo_ref_from_url(final_url)
    except Exception as exc:
        logger.debug(
            "TikTok short-link resolve failed for %s: %s",
            safe_url_for_log(url),
            exc,
        )
        resolved = None

    with _short_link_lock:
        _short_link_cache[url] = (now + _SHORT_LINK_TTL_SECONDS, resolved)
        # Bound cache size.
        if len(_short_link_cache) > 256:
            oldest_key = next(iter(_short_link_cache))
            _short_link_cache.pop(oldest_key, None)
    return resolved


def detect_tiktok_photo_post(url: str) -> TikTokPhotoRef | None:
    """Return a photo-post ref for TikTok /photo/ URLs (and resolved short links).

    Canonical ``tiktok.com/@user/photo/<id>`` URLs need no network. Short hosts
    (``vt.tiktok.com`` / ``vm.tiktok.com``) are resolved once and memoized.
    """
    stripped = url.strip()
    if not stripped:
        return None
    direct = _photo_ref_from_url(stripped)
    if direct is not None:
        return direct
    try:
        hostname = urlsplit(stripped).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    host = hostname.strip().lower().rstrip(".").removeprefix("www.")
    if host not in _TIKTOK_SHORT_HOSTS:
        return None
    return _resolve_short_link(stripped)


def clear_short_link_cache() -> None:
    """Test helper: drop memoized short-link resolutions."""
    with _short_link_lock:
        _short_link_cache.clear()


def _pick_image_url(image_entry: Any) -> str | None:
    if not isinstance(image_entry, dict):
        return None
    image_url = image_entry.get("imageURL")
    if not isinstance(image_url, dict):
        return None
    url_list = image_url.get("urlList")
    if not isinstance(url_list, list):
        return None
    for candidate in url_list:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


def extract_slideshow(
    ref: TikTokPhotoRef,
    *,
    max_images: int = DEFAULT_SLIDESHOW_MAX_IMAGES,
    https_only: bool = False,
) -> SlideshowSource:
    """Extract image URLs and music from a TikTok photo post.

    Uses yt-dlp's TikTok extractor internals (``_extract_web_data_and_status``)
    because the public extract path discards ``imagePost`` slides.
    """
    try:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ie = ydl.get_info_extractor("TikTok")
            ie.initialize()
            # TikTokIE internals: public extract discards imagePost slides.
            web_url = ie._create_url(ref.user.lstrip("@"), ref.video_id)  # type: ignore[attr-defined]
            item, status = ie._extract_web_data_and_status(  # type: ignore[attr-defined]
                web_url, ref.video_id, fatal=False
            )
    except DownloadError:
        raise
    except Exception as exc:
        logger.warning(
            "TikTok slideshow extract failed for %s: %s",
            ref.canonical_url,
            exc,
        )
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED) from exc

    if not isinstance(item, dict) or status not in (0, None):
        raise DownloadError(strings.DOWNLOAD_EXTRACT_FAILED)

    image_post = item.get("imagePost")
    if not isinstance(image_post, dict):
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)
    raw_images = image_post.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    image_urls: list[str] = []
    for entry in raw_images:
        if len(image_urls) >= max_images:
            break
        url = _pick_image_url(entry)
        if url is None:
            continue
        # CDN hosts are not on ALLOWED_MEDIA_HOSTS; only SSRF-check them.
        if not is_safe_media_url(url, https_only=https_only):
            logger.warning(
                "Skipping unsafe slideshow image URL: %s", safe_url_for_log(url)
            )
            continue
        image_urls.append(url)

    if not image_urls:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    audio_url: str | None = None
    audio_duration: float | None = None
    music = item.get("music")
    if isinstance(music, dict):
        play_url = music.get("playUrl")
        if isinstance(play_url, str) and play_url.startswith("http"):
            if is_safe_media_url(play_url, https_only=https_only):
                audio_url = play_url
            else:
                logger.warning(
                    "Ignoring unsafe slideshow audio URL: %s",
                    safe_url_for_log(play_url),
                )
        dur_raw = music.get("duration")
        if isinstance(dur_raw, (int, float)) and float(dur_raw) > 0:
            audio_duration = float(dur_raw)

    title_raw = item.get("desc")
    title = str(title_raw or f"tiktok-{ref.video_id}")[:64]

    return SlideshowSource(
        image_urls=tuple(image_urls),
        audio_url=audio_url,
        title=title,
        canonical_url=ref.canonical_url,
        audio_duration=audio_duration,
    )


def _guess_ext(url: str, default: str) -> str:
    path = urlsplit(url).path.lower()
    for ext in _IMAGE_EXTENSIONS | {".mp3", ".m4a", ".aac", ".mp4", ".wav"}:
        if path.endswith(ext):
            return ext.lstrip(".")
    # TikTok CDN image URLs often omit a file extension.
    if "photomode" in path or "image" in path:
        return "jpg"
    return default


def _download_bytes(
    ydl: yt_dlp.YoutubeDL,
    url: str,
    dest: Path,
    *,
    remaining_budget: int,
    abort_event: threading.Event | None,
) -> int:
    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    if remaining_budget <= 0:
        raise DownloadError(
            strings.DOWNLOAD_EXCEEDS_LIMIT.format(max_mb=1)
        )

    request = Request(
        url,
        extensions={"impersonate": ImpersonateTarget("chrome")},
    )
    try:
        response = ydl.urlopen(request)  # type: ignore[arg-type]
    except Exception as exc:
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    written = 0
    try:
        with dest.open("wb") as out, response:
            while True:
                if abort_event is not None and abort_event.is_set():
                    raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining_budget:
                    raise DownloadError(
                        strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                            max_mb=max(1, remaining_budget // (1024 * 1024))
                        )
                    )
                out.write(chunk)
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    if written <= 0:
        dest.unlink(missing_ok=True)
        raise DownloadError(strings.DOWNLOAD_EMPTY_FILE)
    return written


def plan_slideshow_timeline(
    image_count: int,
    per_slide: float,
    audio_duration: float | None,
    *,
    images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    max_slots: int = 120,
) -> tuple[float, list[float]]:
    """Plan total length and per-slot durations.

    When ``images_loop`` is true (default): if audio is present the video matches
    the full audio length. Images are shown at ``per_slide`` (looping as needed);
    if there are more images than the preferred cadence allows, slide duration is
    shortened so every image appears at least once within the audio.

    When ``images_loop`` is false: one pass through the images at ``per_slide``,
    then trim the audio to that length. A single-image post still uses the full
    audio (same as looping for ``image_count == 1``).
    """
    if image_count < 1:
        raise ValueError("image_count must be >= 1")
    per_slide = max(0.5, per_slide)

    # Single-image posts always fit the full audio when available.
    fit_full_audio = (
        audio_duration is not None
        and audio_duration > 0
        and (images_loop or image_count == 1)
    )

    if fit_full_audio:
        assert audio_duration is not None
        total = float(audio_duration)
        if image_count * per_slide > total:
            # Fit every unique image into the audio window.
            slot = total / image_count
            return total, [slot] * image_count

        slots = max(1, math.ceil(total / per_slide))
        if slots > max_slots:
            slot = total / max_slots
            return total, [slot] * max_slots

        if slots == 1:
            return total, [total]

        durations = [per_slide] * (slots - 1)
        last = total - per_slide * (slots - 1)
        if last < 0.05:
            durations[-1] += last
        else:
            durations.append(last)
        return total, durations

    # images_loop=false with 2+ images (or no audio): one pass, trim audio.
    total = image_count * per_slide
    return total, [per_slide] * image_count


def _cycle_images(image_paths: list[Path], slot_count: int) -> list[Path]:
    n = len(image_paths)
    if n == 0:
        return []
    return [image_paths[i % n] for i in range(slot_count)]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _write_rgba_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    """Write a minimal 8-bit RGBA PNG (stdlib only — no Pillow)."""
    if len(rgba) != width * height * 4:
        raise ValueError("rgba buffer size mismatch")
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)  # filter: None
        rows.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _fill_circle(
    buf: bytearray,
    *,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    rgba: tuple[int, int, int, int],
) -> None:
    r2 = radius * radius
    r, g, b, a = rgba
    for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
        dy = y - cy
        for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
            dx = x - cx
            if dx * dx + dy * dy <= r2:
                i = (y * width + x) * 4
                buf[i] = r
                buf[i + 1] = g
                buf[i + 2] = b
                buf[i + 3] = a


def _nav_dot_radius(unique_count: int, frame_width: int) -> int:
    """Scale dots to TikTok-like size; shrink when many slides."""
    base = max(4, frame_width // 135)  # ~8px at 1080
    if unique_count <= 8:
        return base
    if unique_count <= 15:
        return max(3, base - 1)
    return max(3, base - 2)


def render_nav_dot_png(
    path: Path,
    *,
    unique_count: int,
    active_index: int,
    frame_width: int,
) -> None:
    """Render a transparent strip of page dots (active = solid white).

    Matches TikTok photo-post pagination: inactive dots are soft white,
    the current slide's dot is bright solid white.
    """
    if unique_count < 2:
        raise ValueError("nav dots require at least 2 images")
    active = active_index % unique_count
    radius = _nav_dot_radius(unique_count, frame_width)
    gap = max(radius * 2 + 4, int(radius * 3.2))  # center-to-center
    pad_x = radius + 4
    pad_y = radius + 6
    width = pad_x * 2 + gap * (unique_count - 1) + 1
    height = pad_y * 2 + 1
    # Cap strip width; if too wide, tighten gap.
    max_w = int(frame_width * 0.85)
    if width > max_w and unique_count > 1:
        gap = max(radius * 2 + 2, (max_w - pad_x * 2) // (unique_count - 1))
        width = pad_x * 2 + gap * (unique_count - 1) + 1

    buf = bytearray(width * height * 4)  # transparent
    cy = height // 2
    start_x = pad_x
    inactive = (255, 255, 255, 115)
    active_rgba = (255, 255, 255, 255)
    for i in range(unique_count):
        cx = start_x + i * gap
        _fill_circle(
            buf,
            width=width,
            height=height,
            cx=cx,
            cy=cy,
            radius=radius,
            rgba=active_rgba if i == active else inactive,
        )
    _write_rgba_png(path, width, height, bytes(buf))


def _build_nav_overlays(
    work_dir: Path,
    *,
    unique_count: int,
    frame_width: int,
) -> list[Path]:
    """Create one nav PNG per unique slide index (active highlight differs)."""
    if unique_count < 2:
        return []
    paths: list[Path] = []
    for active in range(unique_count):
        dest = work_dir / f"nav_dots_{active:02d}.png"
        render_nav_dot_png(
            dest,
            unique_count=unique_count,
            active_index=active,
            frame_width=frame_width,
        )
        paths.append(dest)
    return paths


_FFMPEG_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _duration_from_mutagen(path: Path) -> float | None:
    """Return media duration via mutagen, or None on failure."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return None
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return None
    if audio is None:
        return None
    info = getattr(audio, "info", None)
    length = getattr(info, "length", None) if info is not None else None
    if isinstance(length, (int, float)) and length > 0:
        return float(length)
    return None


def _duration_from_ffmpeg(path: Path) -> float | None:
    """Parse duration from ``ffmpeg -i`` stderr (no ffprobe required)."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(path)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    match = _FFMPEG_DURATION_RE.search(stderr)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    try:
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
    return value if value > 0 else None


def _probe_media_duration(path: Path) -> float | None:
    """Return media duration in seconds, or None on failure.

    Prefers mutagen (already a yt-dlp[default] dep); falls back to parsing
    ``ffmpeg -i`` stderr so the image need not ship ffprobe.
    """
    return _duration_from_mutagen(path) or _duration_from_ffmpeg(path)


def _build_ffmpeg_argv(
    *,
    ffmpeg_bin: str,
    image_paths: list[Path],
    audio_path: Path | None,
    output_path: Path,
    slide_durations: list[float],
    total: float,
    width: int,
    height: int,
    crf: int,
    unique_image_count: int | None = None,
    nav_overlay_paths: list[Path] | None = None,
) -> list[str]:
    if len(image_paths) != len(slide_durations):
        raise ValueError("image_paths and slide_durations length mismatch")

    unique_n = unique_image_count if unique_image_count is not None else len(image_paths)
    nav_paths = nav_overlay_paths or []
    use_nav = unique_n >= 2 and len(nav_paths) == unique_n

    argv: list[str] = [ffmpeg_bin, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    n = len(image_paths)
    for path, slide_t in zip(image_paths, slide_durations, strict=True):
        argv.extend(["-loop", "1", "-t", f"{slide_t:.3f}", "-i", str(path)])
    for nav_path in nav_paths if use_nav else []:
        # Loop still overlays for the full slide duration.
        argv.extend(["-loop", "1", "-i", str(nav_path)])
    if audio_path is not None:
        # Play the full audio once (video length is planned to match).
        argv.extend(["-i", str(audio_path)])

    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:-1:-1:color=black,setsar=1,fps=30"
    )
    # TikTok places page dots just above the bottom UI chrome.
    nav_bottom_margin = max(36, height // 28)
    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    nav_input_base = n  # first nav overlay input index
    for idx in range(n):
        if use_nav:
            active = idx % unique_n
            nav_in = nav_input_base + active
            filter_parts.append(
                f"[{idx}:v]{scale}[b{idx}];"
                f"[b{idx}][{nav_in}:v]overlay=(W-w)/2:H-h-{nav_bottom_margin}:shortest=1[v{idx}]"
            )
        else:
            filter_parts.append(f"[{idx}:v]{scale}[v{idx}]")
        concat_inputs.append(f"[v{idx}]")
    filter_parts.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=0[v]")
    filter_complex = ";".join(filter_parts)

    argv.extend(["-filter_complex", filter_complex, "-map", "[v]"])
    audio_input_index = n + (unique_n if use_nav else 0)
    if audio_path is not None:
        argv.extend(["-map", f"{audio_input_index}:a", "-c:a", "aac", "-b:a", "128k"])
    else:
        argv.extend(["-an"])
    argv.extend(
        [
            "-t",
            f"{total:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return argv


def _run_ffmpeg(
    argv: list[str],
    *,
    timeout_seconds: float,
    abort_event: threading.Event | None,
) -> None:
    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=max(0.1, timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED) from exc
    except OSError as exc:
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED) from exc

    if abort_event is not None and abort_event.is_set():
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")[-500:]
        logger.warning("ffmpeg slideshow failed (code %s): %s", completed.returncode, stderr)
        raise DownloadError(strings.SLIDESHOW_BUILD_FAILED)


def build_slideshow_video(
    source: SlideshowSource,
    work_dir: Path,
    *,
    max_file_bytes: int,
    timeout_seconds: int,
    slide_ms: int = DEFAULT_SLIDESHOW_SLIDE_MS,
    images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    abort_event: threading.Event | None = None,
    https_only: bool = False,
) -> DownloadedMedia:
    """Download slides (+ optional audio) and mux into an MP4 via ffmpeg."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise DownloadError(strings.SLIDESHOW_FFMPEG_MISSING)

    if not source.image_urls:
        raise DownloadError(strings.SLIDESHOW_NO_IMAGES)

    per_slide = max(0.5, slide_ms / 1000.0)

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": min(30, timeout_seconds),
    }

    image_paths: list[Path] = []
    audio_path: Path | None = None
    bytes_used = 0
    started = time.monotonic()

    def _remaining_timeout() -> float:
        return max(0.1, float(timeout_seconds) - (time.monotonic() - started))

    try:
        with _safe_dns_resolution():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                for idx, image_url in enumerate(source.image_urls):
                    if abort_event is not None and abort_event.is_set():
                        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
                    if https_only and not image_url.lower().startswith("https://"):
                        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
                    ext = _guess_ext(image_url, "jpg")
                    dest = work_dir / f"slide_{idx:03d}.{ext}"
                    written = _download_bytes(
                        ydl,
                        image_url,
                        dest,
                        remaining_budget=max_file_bytes - bytes_used,
                        abort_event=abort_event,
                    )
                    bytes_used += written
                    image_paths.append(dest)

                if source.audio_url:
                    if https_only and not source.audio_url.lower().startswith("https://"):
                        raise DownloadError(strings.DOWNLOAD_HTTPS_REQUIRED)
                    audio_ext = _guess_ext(source.audio_url, "mp3")
                    audio_dest = work_dir / f"audio.{audio_ext}"
                    written = _download_bytes(
                        ydl,
                        source.audio_url,
                        audio_dest,
                        remaining_budget=max_file_bytes - bytes_used,
                        abort_event=abort_event,
                    )
                    bytes_used += written
                    audio_path = audio_dest
    except DownloadError:
        raise
    except Exception as exc:
        logger.warning(
            "Slideshow asset download failed for %s: %s",
            safe_url_for_log(source.canonical_url),
            exc,
        )
        raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC) from exc

    audio_duration = source.audio_duration
    if audio_path is not None and (audio_duration is None or audio_duration <= 0):
        audio_duration = _probe_media_duration(audio_path)

    total, slide_durations = plan_slideshow_timeline(
        len(image_paths),
        per_slide,
        audio_duration if audio_path is not None else None,
        images_loop=images_loop,
    )
    sequenced_images = _cycle_images(image_paths, len(slide_durations))
    unique_count = len(image_paths)
    # Half-up so fractional seconds round sensibly for Telegram's int duration.
    duration = max(1, math.floor(total + 0.5))

    output_path = work_dir / "slideshow.mp4"
    # One source render; the shared downloader applies at most two bounded
    # Telegram-size optimization passes if this output is still too large.
    encode_attempts: list[tuple[int, int, int]] = [(1080, 1920, 24)]

    last_error: Exception | None = None
    for width, height, crf in encode_attempts:
        if abort_event is not None and abort_event.is_set():
            raise DownloadError(strings.DOWNLOAD_FAILED_GENERIC)
        output_path.unlink(missing_ok=True)
        try:
            nav_overlays = _build_nav_overlays(
                work_dir,
                unique_count=unique_count,
                frame_width=width,
            )
        except Exception as exc:
            logger.warning("Could not build slideshow nav dots: %s", exc)
            nav_overlays = []
        argv = _build_ffmpeg_argv(
            ffmpeg_bin=ffmpeg_bin,
            image_paths=sequenced_images,
            audio_path=audio_path,
            output_path=output_path,
            slide_durations=slide_durations,
            total=total,
            width=width,
            height=height,
            crf=crf,
            unique_image_count=unique_count,
            nav_overlay_paths=nav_overlays,
        )
        try:
            _run_ffmpeg(
                argv,
                timeout_seconds=_remaining_timeout(),
                abort_event=abort_event,
            )
        except DownloadError as exc:
            last_error = exc
            continue

        if not output_path.exists() or output_path.stat().st_size <= 0:
            last_error = DownloadError(strings.SLIDESHOW_BUILD_FAILED)
            continue

        size = output_path.stat().st_size
        if size > max_file_bytes:
            logger.info(
                "Slideshow source output %s bytes exceeds bounded source limit",
                size,
            )
            last_error = DownloadError(
                strings.DOWNLOAD_EXCEEDS_LIMIT.format(
                    max_mb=max_file_bytes // (1024 * 1024)
                )
            )
            continue

        return DownloadedMedia(
            path=output_path,
            title=source.title,
            kind=MediaKind.VIDEO,
            duration=duration,
        )

    if last_error is not None:
        raise last_error
    raise DownloadError(strings.SLIDESHOW_BUILD_FAILED)


def download_tiktok_slideshow(
    url: str,
    work_dir: Path,
    *,
    max_file_bytes: int,
    timeout_seconds: int,
    slide_ms: int = DEFAULT_SLIDESHOW_SLIDE_MS,
    max_images: int = DEFAULT_SLIDESHOW_MAX_IMAGES,
    images_loop: bool = DEFAULT_SLIDESHOW_IMAGES_LOOP,
    abort_event: threading.Event | None = None,
    https_only: bool = False,
    allowed_hosts: frozenset[str] | None = None,
) -> DownloadedMedia | None:
    """If *url* is a TikTok photo post, build and return a slideshow video.

    Returns ``None`` when the URL is not a photo post (caller should fall through
    to the normal yt-dlp path).
    """
    _ = allowed_hosts  # user URL already checked by caller
    ref = detect_tiktok_photo_post(url)
    if ref is None:
        return None
    source = extract_slideshow(
        ref,
        max_images=max_images,
        https_only=https_only,
    )
    return build_slideshow_video(
        source,
        work_dir,
        max_file_bytes=max_file_bytes,
        timeout_seconds=timeout_seconds,
        slide_ms=slide_ms,
        images_loop=images_loop,
        abort_event=abort_event,
        https_only=https_only,
    )
