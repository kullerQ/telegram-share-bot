"""Unit tests for in-process extract_info memoization."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yt_dlp

from telegram_share_bot.downloader import (
    _EXTRACT_INFO_CACHE_MAX,
    _EXTRACT_INFO_TTL_SECONDS,
    DownloadError,
    _download_sync,
    _extract_info_cache,
    _extract_info_cached,
    _looks_like_stale_cdn_url,
    clear_extract_info_cache,
)


class TestExtractInfoCache(unittest.TestCase):
    def setUp(self) -> None:
        clear_extract_info_cache()

    def tearDown(self) -> None:
        clear_extract_info_cache()

    def test_second_call_hits_cache(self) -> None:
        ydl = MagicMock()
        ydl.extract_info.return_value = {"id": "abc", "title": "t", "url": "https://cdn.example/v.mp4"}

        first, from_cache_1 = _extract_info_cached(ydl, "https://example.com/v")
        second, from_cache_2 = _extract_info_cached(ydl, "https://example.com/v")

        self.assertFalse(from_cache_1)
        self.assertTrue(from_cache_2)
        self.assertEqual(first["id"], "abc")
        self.assertEqual(second["id"], "abc")
        self.assertEqual(ydl.extract_info.call_count, 1)
        # Cache hit must return an independent copy.
        second["id"] = "mutated"
        third, _ = _extract_info_cached(ydl, "https://example.com/v")
        self.assertEqual(third["id"], "abc")

    def test_force_refresh_bypasses_cache(self) -> None:
        ydl = MagicMock()
        ydl.extract_info.side_effect = [
            {"id": "old", "title": "a"},
            {"id": "new", "title": "b"},
        ]

        _extract_info_cached(ydl, "https://example.com/v")
        refreshed, from_cache = _extract_info_cached(
            ydl, "https://example.com/v", force_refresh=True
        )

        self.assertFalse(from_cache)
        self.assertEqual(refreshed["id"], "new")
        self.assertEqual(ydl.extract_info.call_count, 2)

    def test_ttl_expiry_reextracts(self) -> None:
        ydl = MagicMock()
        ydl.extract_info.side_effect = [
            {"id": "old"},
            {"id": "fresh"},
        ]
        url = "https://example.com/ttl"

        with patch(
            "telegram_share_bot.downloader.time.monotonic",
            side_effect=[
                100.0,
                100.0 + _EXTRACT_INFO_TTL_SECONDS + 1,
            ],
        ):
            _extract_info_cached(ydl, url)
            result, from_cache = _extract_info_cached(ydl, url)

        self.assertFalse(from_cache)
        self.assertEqual(result["id"], "fresh")
        self.assertEqual(ydl.extract_info.call_count, 2)

    def test_cache_bounded(self) -> None:
        ydl = MagicMock()

        def _info(url: str) -> dict[str, str]:
            return {"id": url}

        ydl.extract_info.side_effect = lambda url, download=False: _info(url)

        for i in range(_EXTRACT_INFO_CACHE_MAX + 10):
            _extract_info_cached(ydl, f"https://example.com/{i}")

        self.assertLessEqual(len(_extract_info_cache), _EXTRACT_INFO_CACHE_MAX)

    def test_stale_cdn_detector(self) -> None:
        self.assertTrue(
            _looks_like_stale_cdn_url(
                Exception("ERROR: unable to download video data: HTTP Error 403: Forbidden")
            )
        )
        self.assertTrue(
            _looks_like_stale_cdn_url(Exception("URL has expired"))
        )
        self.assertFalse(
            _looks_like_stale_cdn_url(Exception("File is larger than max-filesize"))
        )
        self.assertFalse(
            _looks_like_stale_cdn_url(Exception("Unsupported URL"))
        )

    def test_download_retries_once_on_stale_cached_extract(self) -> None:
        url = "https://www.youtube.com/watch?v=stale123"
        info = {
            "id": "stale123",
            "title": "Clip",
            "ext": "mp4",
            "url": "https://cdn.example/expired.mp4",
        }
        fresh_info = {
            "id": "stale123",
            "title": "Clip",
            "ext": "mp4",
            "url": "https://cdn.example/fresh.mp4",
        }

        mock_ydl = MagicMock()
        # First extract (cache miss via direct path simulation) + refresh.
        mock_ydl.extract_info.side_effect = [info, fresh_info]

        calls = {"n": 0}

        def process_ie_result(extracted: dict, download: bool = True) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise yt_dlp.utils.DownloadError(
                    "ERROR: unable to download video data: HTTP Error 403: Forbidden"
                )
            # Simulate a completed download on the retry.
            return {**extracted, "requested_downloads": []}

        mock_ydl.process_ie_result.side_effect = process_ie_result
        mock_ydl.prepare_filename.return_value = "clip.mp4"

        with tempfile.TemporaryDirectory() as tmp_dir:
            download_dir = Path(tmp_dir)

            def fake_resolve(info, work_dir, ydl):
                path = work_dir / "clip.mp4"
                path.write_bytes(b"video-bytes")
                return path

            with patch("yt_dlp.YoutubeDL") as mock_cls:
                mock_cls.return_value.__enter__.return_value = mock_ydl
                with patch(
                    "telegram_share_bot.downloader._resolve_downloaded_path",
                    side_effect=fake_resolve,
                ):
                    # Seed cache as if get_direct_stream already extracted.
                    _extract_info_cached(mock_ydl, url)
                    self.assertEqual(mock_ydl.extract_info.call_count, 1)

                    media = _download_sync(
                        url=url,
                        download_dir=download_dir,
                        max_file_bytes=10 * 1024 * 1024,
                        timeout_seconds=30,
                        abort_event=threading.Event(),
                    )

            self.assertEqual(media.title, "Clip")
            # Seed + one forced refresh.
            self.assertEqual(mock_ydl.extract_info.call_count, 2)
            self.assertEqual(mock_ydl.process_ie_result.call_count, 2)

    def test_download_does_not_retry_non_stale_errors(self) -> None:
        url = "https://www.youtube.com/watch?v=big123"
        info = {"id": "big123", "title": "Huge", "ext": "mp4"}

        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = info
        mock_ydl.process_ie_result.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: File is larger than max-filesize"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("yt_dlp.YoutubeDL") as mock_cls:
                mock_cls.return_value.__enter__.return_value = mock_ydl
                with self.assertRaises(DownloadError):
                    _download_sync(
                        url=url,
                        download_dir=Path(tmp_dir),
                        max_file_bytes=1024,
                        timeout_seconds=30,
                    )

        # Only the initial extract — no refresh retry.
        self.assertEqual(mock_ydl.extract_info.call_count, 1)
        self.assertEqual(mock_ydl.process_ie_result.call_count, 1)


if __name__ == "__main__":
    unittest.main()
