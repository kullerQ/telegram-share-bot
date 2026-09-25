"""Checks that inline thumbnails use a known platform host."""

from __future__ import annotations

import unittest
from pathlib import Path

from telegram_share_bot.platform_icons import (
    _LOGO_BY_HOST,
    thumbnail_url,
    video_thumbnail_url,
)


class TestPlatformIcons(unittest.TestCase):
    def test_each_mapped_logo_exists_in_the_package(self) -> None:
        asset_dir = (
            Path(__file__).resolve().parents[1]
            / "telegram_share_bot"
            / "assets"
            / "platforms"
        )
        for filename in set(_LOGO_BY_HOST.values()):
            with self.subTest(filename=filename):
                self.assertTrue((asset_dir / filename).is_file())

    def test_known_platforms_and_host_aliases(self) -> None:
        base = "https://images.example.org/platforms"
        self.assertEqual(
            thumbnail_url("https://youtu.be/example", base),
            f"{base}/youtube.png",
        )
        self.assertEqual(
            thumbnail_url("https://www.tiktok.com/@user/photo/123", base),
            f"{base}/tiktok.png",
        )
        self.assertEqual(
            thumbnail_url("https://x.com/user/status/123", base),
            f"{base}/twitter.png",
        )
        self.assertIsNone(thumbnail_url("https://notyoutube.com/watch", base))
        self.assertIsNone(thumbnail_url("https://youtube.com/watch", None))
        upstream = thumbnail_url("https://youtu.be/example", "upstream")
        self.assertIsNotNone(upstream)
        assert upstream is not None
        self.assertIn("/224pxl/round%20square/youtube224.png", upstream)
        self.assertIn("/bilibili28c.png", thumbnail_url("https://bilibili.com/video", "upstream"))

    def test_youtube_video_preview_uses_id_without_network_lookup(self) -> None:
        for url in (
            "https://youtu.be/GKq9nKZpmu0?t=150",
            "https://www.youtube.com/watch?v=GKq9nKZpmu0&feature=share",
            "https://youtube.com/shorts/GKq9nKZpmu0",
            "https://www.youtube-nocookie.com/embed/GKq9nKZpmu0",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    video_thumbnail_url(url),
                    "https://i.ytimg.com/vi/GKq9nKZpmu0/mqdefault.jpg",
                )

    def test_unrecognized_video_links_have_no_derived_preview(self) -> None:
        for url in (
            "https://youtube.com/watch?v=invalid",
            "https://notyoutube.com/watch?v=GKq9nKZpmu0",
            "https://youtube.com.evil.example/watch?v=GKq9nKZpmu0",
            "https://www.tiktok.com/@user/video/123456",
        ):
            with self.subTest(url=url):
                self.assertIsNone(video_thumbnail_url(url))


if __name__ == "__main__":
    unittest.main()
