"""Unit tests for SSRF prevention and URL security validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_share_bot import strings
from telegram_share_bot.downloader import (
    DownloadError,
    download_media,
    get_direct_stream,
    is_safe_media_url,
)


class TestSsrfProtection(unittest.IsolatedAsyncioTestCase):
    def test_unsafe_urls_rejected(self) -> None:
        unsafe_urls = [
            "http://127.0.0.1:8000/secret",
            "http://127.0.0.2:80/",
            "http://localhost:3000/",
            "http://test.localhost/",
            "http://app.local/",
            "http://service.internal/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/admin",
            "http://172.16.0.5/status",
            "http://192.168.1.1/router",
            "http://[::1]/",
            "file:///etc/passwd",
            "ftp://example.com/file.mp4",
            "gopher://example.com",
            "",
            "   ",
        ]
        for url in unsafe_urls:
            with self.subTest(url=url):
                self.assertFalse(is_safe_media_url(url))

    def test_safe_urls_accepted(self) -> None:
        safe_urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://x.com/user/status/123456",
            "https://instagram.com/reel/12345",
            "http://8.8.8.8/test.mp4",
        ]
        for url in safe_urls:
            with self.subTest(url=url):
                self.assertTrue(is_safe_media_url(url))

    async def test_download_media_blocks_ssrf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(DownloadError) as ctx:
                await download_media(
                    url="http://127.0.0.1:8000/video.mp4",
                    download_dir=Path(tmp_dir),
                    max_file_bytes=1024 * 1024,
                    timeout_seconds=5,
                )
            self.assertEqual(str(ctx.exception), strings.DOWNLOAD_UNSAFE_URL)

    async def test_get_direct_stream_blocks_ssrf(self) -> None:
        stream = await get_direct_stream(
            url="http://169.254.169.254/latest/meta-data/",
            max_file_bytes=1024 * 1024,
            timeout_seconds=5,
        )
        self.assertIsNone(stream)


if __name__ == "__main__":
    unittest.main()
