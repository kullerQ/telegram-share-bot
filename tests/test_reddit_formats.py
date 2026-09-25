"""Regression checks for Reddit videos without a separate audio track."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_share_bot.downloader import (
    _download_sync,
    _extract_direct_stream_sync,
)


class TestRedditFormats(unittest.TestCase):
    def test_oversized_selected_stream_is_not_sent_directly(self) -> None:
        selected = {
            "id": "0y05lfnywgrh1",
            "url": "https://v.redd.it/0y05lfnywgrh1/CMAF_1080.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "none",
            "filesize_approx": 78_750_000,
        }
        with (
            patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
            patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
            patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL") as ydl,
            patch(
                "telegram_share_bot.downloader._extract_info_cached",
                return_value=(selected, False),
            ),
        ):
            ydl.return_value.__enter__.return_value = MagicMock()
            result = _extract_direct_stream_sync(
                "https://reddit.com/r/BitchImATrain/s/08YQHJYdXQ",
                max_file_bytes=45 * 1024 * 1024,
            )
        self.assertIsNone(result)

    def test_full_download_selector_includes_silent_https_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            file_path = work_dir / "silent.mp4"
            file_path.write_bytes(b"video")
            options: list[dict[str, object]] = []

            class FakeYdl:
                def __init__(self, opts: dict[str, object]) -> None:
                    options.append(opts)
                    self.params = dict(opts)

                def __enter__(self) -> FakeYdl:
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

                def build_format_selector(self, selector: str) -> str:
                    return selector

                def process_ie_result(
                    self, info: dict[str, object], download: bool = True
                ) -> dict[str, object]:
                    return info

                def prepare_filename(self, info: dict[str, object]) -> str:
                    return str(file_path)

            with (
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
                patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", FakeYdl),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=({"title": "Silent", "ext": "mp4"}, False),
                ),
                patch(
                    "telegram_share_bot.downloader._resolve_downloaded_path",
                    return_value=file_path,
                ),
            ):
                _download_sync(
                    "https://reddit.com/r/BitchImATrain/s/08YQHJYdXQ",
                    work_dir,
                    max_file_bytes=45 * 1024 * 1024,
                    timeout_seconds=30,
                )
            self.assertIn(
                "bv*[acodec=none][protocol=https]",
                str(options[0]["format"]),
            )
            self.assertNotIn("height<=720", str(options[0]["format"]))
            self.assertEqual(options[0]["max_filesize"], 90 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
