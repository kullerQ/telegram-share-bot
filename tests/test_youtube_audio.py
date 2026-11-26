"""Regression coverage for original YouTube soundtrack selection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from telegram_share_bot.media.clips import _youtube_hls_clip_streams
from telegram_share_bot.media.direct import _extract_direct_stream_sync
from telegram_share_bot.media.formats import select_download_candidates
from telegram_share_bot.media.models import MediaFormat, TimeRange, VideoQualityPolicy
from telegram_share_bot.media.transfer import _download_sync
from telegram_share_bot.storage.media_cache import _cache_key

YOUTUBE_URL = "https://www.youtube.com/watch?v=abcdefghijk"


def track(format_id: str, preference: int, bitrate: int, size: int) -> dict:
    return {
        "format_id": format_id,
        "url": f"https://cdn.example/{format_id}.m4a",
        "protocol": "https",
        "ext": "m4a",
        "vcodec": "none",
        "acodec": "aac",
        "abr": bitrate,
        "filesize": size,
        "language_preference": preference,
        "resolution": "audio only",
    }


class TestYouTubeAudio(unittest.TestCase):
    def test_original_audio_outranks_higher_bitrate_and_muxed_dubs(self) -> None:
        original = track("original", 10, 96, 21)
        dub = track("dub", 5, 128, 25)
        video = {"format_id": "video", "vcodec": "h264", "acodec": "none", "height": 720}
        muxed_dub = {**video, "format_id": "muxed-dub", "acodec": "aac", "height": 1080}
        info = {"formats": [original, dub, video, muxed_dub]}

        for media_format, quality_policy, expected in (
            (MediaFormat.AUDIO, VideoQualityPolicy.AUTO, ["original", "dub"]),
            (MediaFormat.VIDEO, VideoQualityPolicy.AUTO, ["video+original", "muxed-dub"]),
            (MediaFormat.VIDEO, VideoQualityPolicy.BEST, ["video+original", "muxed-dub"]),
            (MediaFormat.VIDEO, VideoQualityPolicy.BALANCED, ["video+original", "muxed-dub"]),
        ):
            with self.subTest(media_format=media_format, quality_policy=quality_policy):
                _, _, selectors = select_download_candidates(
                    info,
                    time_range=None,
                    media_format=media_format,
                    quality_policy=quality_policy,
                    max_file_bytes=100,
                    source_limit=200,
                    prefer_original_audio=True,
                )
                self.assertEqual(selectors[:2], expected)

    def test_video_pairing_checks_size_before_falling_back_to_dub(self) -> None:
        original_high = track("original-high", 10, 128, 80)
        original_low = track("original-low", 10, 64, 30)
        dub = track("dub", 5, 96, 20)
        video = {
            "format_id": "video",
            "vcodec": "h264",
            "acodec": "none",
            "filesize": 150,
        }
        for tracks, expected in (
            ([original_high, original_low, dub], ["video+original-low", "video+dub"]),
            ([original_high, dub], ["video+dub"]),
        ):
            with self.subTest(expected=expected):
                _, _, selectors = select_download_candidates(
                    {"formats": [video, *tracks]},
                    time_range=None,
                    media_format=MediaFormat.VIDEO,
                    quality_policy=VideoQualityPolicy.AUTO,
                    max_file_bytes=100,
                    source_limit=200,
                    prefer_original_audio=True,
                )
                self.assertEqual(selectors, expected)

    def test_download_retries_with_dub_when_original_cannot_be_downloaded(self) -> None:
        original = track("original", 10, 96, 21)
        dub = track("dub", 5, 128, 25)
        info = {
            "id": "abcdefghijk",
            "title": "Example",
            "extractor": "youtube",
            "formats": [original, dub],
        }
        attempts: list[str] = []

        def download(filename: str, selected: dict) -> tuple[bool, bool]:
            attempts.append(selected["format_id"])
            if selected["format_id"] == "original":
                raise yt_dlp.utils.DownloadError("Audio stream unavailable")
            Path(filename).write_bytes(b"fallback")
            return True, True

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch("telegram_share_bot.media.transfer.is_safe_media_url", return_value=True),
            patch(
                "telegram_share_bot.media.transfer._extract_info_cached",
                return_value=(info, False),
            ),
            patch.object(yt_dlp.YoutubeDL, "dl", side_effect=download),
        ):
            media = _download_sync(
                YOUTUBE_URL, Path(tmp_dir), 100, 10, media_format=MediaFormat.AUDIO,
            )
            self.assertEqual(media.path.read_bytes(), b"fallback")
        self.assertEqual(attempts, ["original", "dub"])

    def test_direct_audio_prioritizes_original_and_defers_incompatible_original(self) -> None:
        original = track("original", 10, 96, 21)
        dub = track("dub", 5, 128, 25)
        for formats, expected in (
            ([original, dub], original["url"]),
            ([{**original, "ext": "webm"}, dub], None),
            ([dub], dub["url"]),
        ):
            with (
                self.subTest(expected=expected),
                patch("telegram_share_bot.media.direct.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.media.direct.create_youtube_dl"),
                patch(
                    "telegram_share_bot.media.direct._extract_info_cached",
                    return_value=({**dub, "formats": formats}, False),
                ),
            ):
                result = _extract_direct_stream_sync(
                    YOUTUBE_URL, max_file_bytes=100, media_format=MediaFormat.AUDIO,
                )
                self.assertEqual(result.direct_url if result else None, expected)

    def test_hls_clips_use_original_or_defer_to_generic_downloader(self) -> None:
        original = track("original", 10, 96, 21)
        dub_hls = {**track("dub", 5, 128, 25), "protocol": "m3u8_native"}
        original_hls = {**original, "protocol": "m3u8_native"}
        result = _youtube_hls_clip_streams({"formats": [original_hls, dub_hls]}, 15, 100)
        self.assertEqual(result, (original_hls, []))
        self.assertIsNone(_youtube_hls_clip_streams({"formats": [original, dub_hls]}, 15, 100))
        self.assertEqual(_youtube_hls_clip_streams({"formats": [dub_hls]}, 15, 100), (dub_hls, []))

    def test_only_youtube_keys_bypass_old_soundtrack_cache(self) -> None:
        for media_format in MediaFormat:
            for time_range in (None, TimeRange(15, 30)):
                with self.subTest(media_format=media_format, time_range=time_range):
                    key = _cache_key(YOUTUBE_URL, time_range, media_format=media_format)
                    self.assertIn("&audio=original-first", key)
                    other = _cache_key(
                        "https://example.com/video", time_range, media_format=media_format,
                    )
                    self.assertNotIn("&audio=original-first", other)
