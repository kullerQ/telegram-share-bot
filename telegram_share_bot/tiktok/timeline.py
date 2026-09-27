"""Slideshow timing and TikTok-style navigation overlay helpers."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

from telegram_share_bot.config import DEFAULT_SLIDESHOW_IMAGES_LOOP


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
