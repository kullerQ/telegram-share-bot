"""Unit tests for SSRF prevention and URL security validation."""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot import strings
from telegram_share_bot.downloader import (
    DownloadError,
    _safe_dns_resolution,
    download_media,
    get_direct_stream,
    is_allowed_media_host,
    is_safe_media_url,
)

# Patch the resolver the DNS guard calls — not socket.getaddrinfo itself,
# which is permanently wrapped once the guard is installed.
_RESOLVER = "telegram_share_bot.downloader._REAL_GETADDRINFO"


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
            "http://100.64.1.1/cgnat",
            "http://100.127.255.255/cgnat",
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
                self.assertTrue(is_safe_media_url(url, https_only=False))

    def test_https_only_rejects_http(self) -> None:
        self.assertFalse(
            is_safe_media_url("http://8.8.8.8/test.mp4", https_only=True)
        )
        self.assertTrue(
            is_safe_media_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", https_only=True
            )
        )

    def test_dns_guard_blocks_rebinding_to_private_ip(self) -> None:
        private_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 0),
            )
        ]

        def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            return private_result

        with patch(_RESOLVER, side_effect=fake_getaddrinfo):
            with _safe_dns_resolution():
                with self.assertRaises(OSError):
                    socket.getaddrinfo("evil.example", 80)

    def test_dns_guard_pins_first_safe_ip(self) -> None:
        calls = {"n": 0}
        first = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
        rebound = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ]

        def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            calls["n"] += 1
            return first if calls["n"] == 1 else rebound

        with patch(_RESOLVER, side_effect=fake_getaddrinfo):
            with _safe_dns_resolution():
                first_lookup = socket.getaddrinfo("cdn.example", 443)
                self.assertEqual(first_lookup[0][4][0], "8.8.8.8")
                self.assertEqual(len(first_lookup), 1)
                second_lookup = socket.getaddrinfo("cdn.example", 443)
                self.assertEqual(second_lookup[0][4][0], "8.8.8.8")
                self.assertEqual(len(second_lookup), 1)

    def test_dns_guard_repins_when_pinned_ip_disappears(self) -> None:
        """CDN rotation must re-pin, not abort (TikTok/Akamai return rotating sets)."""
        calls = {"n": 0}
        first = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        rebound = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]

        def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            calls["n"] += 1
            return first if calls["n"] == 1 else rebound

        with patch(_RESOLVER, side_effect=fake_getaddrinfo):
            with _safe_dns_resolution():
                first_lookup = socket.getaddrinfo("cdn.example", 443)
                self.assertEqual(first_lookup[0][4][0], "8.8.8.8")
                second_lookup = socket.getaddrinfo("cdn.example", 443)
                self.assertEqual(second_lookup[0][4][0], "1.1.1.1")
                third_lookup = socket.getaddrinfo("cdn.example", 443)
                self.assertEqual(third_lookup[0][4][0], "1.1.1.1")

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

    def test_media_host_allowlist(self) -> None:
        allowed = frozenset({"youtube.com", "tiktok.com"})
        self.assertTrue(
            is_allowed_media_host(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", allowed
            )
        )
        self.assertTrue(
            is_allowed_media_host("https://m.tiktok.com/@u/video/1", allowed)
        )
        self.assertFalse(
            is_allowed_media_host("https://example.com/video.mp4", allowed)
        )
        self.assertTrue(
            is_allowed_media_host("https://example.com/video.mp4", None)
        )


if __name__ == "__main__":
    unittest.main()
