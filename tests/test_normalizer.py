"""Unit tests for URL normalization and canonicalization."""

from __future__ import annotations

import unittest

from telegram_share_bot.normalizer import normalize_url


class TestNormalizeUrl(unittest.TestCase):
    def test_youtube_variants(self) -> None:
        expected = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        variants = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=abcdef12345",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://youtube.com/shorts/dQw4w9WgXcQ?feature=share",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=123&feature=shared",
        ]
        for url in variants:
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), expected)

    def test_twitter_variants(self) -> None:
        expected = "https://x.com/jack/status/20"
        variants = [
            "https://twitter.com/jack/status/20",
            "https://x.com/jack/status/20",
            "https://mobile.twitter.com/jack/status/20?s=20&t=abcdef",
            "https://vxtwitter.com/jack/status/20?ref_src=twsrc",
            "https://fxtwitter.com/jack/status/20",
        ]
        for url in variants:
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), expected)

    def test_instagram_variants(self) -> None:
        expected_reel = "https://www.instagram.com/reel/C1234567890/"
        self.assertEqual(
            normalize_url("https://www.instagram.com/reel/C1234567890/?igsh=abcdef"),
            expected_reel,
        )
        self.assertEqual(
            normalize_url("https://instagram.com/reel/C1234567890/"),
            expected_reel,
        )

        expected_post = "https://www.instagram.com/p/C1234567890/"
        self.assertEqual(
            normalize_url("https://instagram.com/p/C1234567890/?utm_source=ig_web_copy_link"),
            expected_post,
        )

    def test_tiktok_variants(self) -> None:
        expected = "https://www.tiktok.com/@user/video/7123456789012345678"
        url = (
            "https://www.tiktok.com/@user/video/7123456789012345678"
            "?is_from_webapp=1&sender_device=pc"
        )
        self.assertEqual(normalize_url(url), expected)

    def test_generic_url_strips_tracking(self) -> None:
        url = (
            "https://example.com/video.mp4"
            "?utm_source=newsletter&b=2&utm_medium=email&a=1&fbclid=xyz"
        )
        # Should strip utm_* and fbclid, sort remaining query params a=1&b=2
        expected = "https://example.com/video.mp4?a=1&b=2"
        self.assertEqual(normalize_url(url), expected)


if __name__ == "__main__":
    unittest.main()
