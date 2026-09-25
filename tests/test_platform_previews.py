"""Checks for bounded platform thumbnails and safe logo fallback."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from telegram_share_bot.platform_previews import (
    _PREVIEW_CACHE,
    Preview,
    _lookup_page_image,
    _lookup_reddit,
    _lookup_tiktok,
    _trusted_image_url,
    resolve_preview,
)


class TestPlatformPreviews(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _PREVIEW_CACHE.clear()

    async def test_youtube_preview_needs_no_http_request(self) -> None:
        with patch("telegram_share_bot.platform_previews.httpx.AsyncClient") as client:
            preview = await resolve_preview("https://youtu.be/GKq9nKZpmu0")
        client.assert_not_called()
        self.assertEqual(
            preview,
            Preview("https://i.ytimg.com/vi/GKq9nKZpmu0/mqdefault.jpg", 320, 180),
        )

    async def test_tiktok_oembed_preview_and_dimensions(self) -> None:
        def response(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "www.tiktok.com")
            self.assertEqual(request.url.path, "/oembed")
            return httpx.Response(
                200,
                json={
                    "thumbnail_url": "https://p16-common-sign.tiktokcdn-eu.com/cover.jpg",
                    "thumbnail_width": 576,
                    "thumbnail_height": 1024,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            preview = await _lookup_tiktok("https://www.tiktok.com/@user/video/123456789", client)
        self.assertEqual(
            preview,
            Preview("https://p16-common-sign.tiktokcdn-eu.com/cover.jpg", 576, 1024),
        )

    async def test_x_page_image_and_untrusted_image_fallback(self) -> None:
        def response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><head><meta property="og:image" '
                'content="https://pbs.twimg.com/ext_tw_video_thumb/123/cover.jpg">'
                "</head></html>",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            preview = await _lookup_page_image("https://x.com/user/status/123", "x", client)
        self.assertEqual(
            preview,
            Preview("https://pbs.twimg.com/ext_tw_video_thumb/123/cover.jpg?format=jpg&name=small"),
        )
        self.assertIsNone(_trusted_image_url("http://127.0.0.1/image.jpg", "x"))
        self.assertIsNone(_trusted_image_url("https://evil.example/image.jpg", "x"))

    async def test_instagram_and_facebook_public_page_images(self) -> None:
        for platform, image in (
            ("instagram", "https://scontent.cdninstagram.com/reel.jpg"),
            ("facebook", "https://scontent.fbcdn.net/reel.jpg"),
        ):
            with self.subTest(platform=platform):
                transport = httpx.MockTransport(
                    lambda request, image=image: httpx.Response(
                        200,
                        headers={"content-type": "text/html"},
                        text=f'<head><meta property="og:image" content="{image}"></head>',
                    )
                )
                async with httpx.AsyncClient(transport=transport) as client:
                    preview = await _lookup_page_image(
                        f"https://www.{platform}.com/reel/example", platform, client
                    )
                self.assertEqual(preview, Preview(image))

    async def test_reddit_video_json_preview(self) -> None:
        def response(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/comments/abc123.json")
            return httpx.Response(
                200,
                json=[
                    {
                        "data": {
                            "children": [
                                {
                                    "data": {
                                        "is_video": True,
                                        "preview": {
                                            "images": [
                                                {
                                                    "source": {
                                                        "url": "https://preview.redd.it/abc123.jpg",
                                                        "width": 640,
                                                        "height": 360,
                                                    }
                                                }
                                            ]
                                        },
                                    }
                                }
                            ]
                        }
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            preview = await _lookup_reddit(
                "https://www.reddit.com/r/videos/comments/abc123/example/", client
            )
        self.assertEqual(preview, Preview("https://preview.redd.it/abc123.jpg", 640, 360))

    async def test_x_lookup_preserves_account_case_and_strips_tracking(self) -> None:
        expected = Preview("https://pbs.twimg.com/ext_tw_video_thumb/example.jpg")
        with patch(
            "telegram_share_bot.platform_previews._lookup_page_image",
            new=AsyncMock(return_value=expected),
        ) as lookup:
            preview = await resolve_preview(
                "https://x.com/PunchingCat/status/2103311089614340120?s=20"
            )
        self.assertEqual(preview, expected)
        self.assertEqual(
            lookup.await_args.args[:2],
            ("https://x.com/PunchingCat/status/2103311089614340120", "x"),
        )

    async def test_failed_lookup_is_cached_as_logo_fallback(self) -> None:
        with patch(
            "telegram_share_bot.platform_previews._lookup_page_image",
            new=AsyncMock(return_value=None),
        ) as lookup:
            for _ in range(2):
                self.assertIsNone(await resolve_preview("https://x.com/user/status/123"))
        lookup.assert_awaited_once()

    async def test_slow_lookup_returns_logo_fallback(self) -> None:
        async def slow_lookup(*args: object) -> Preview | None:
            await asyncio.sleep(1)
            return Preview("https://pbs.twimg.com/late.jpg")

        with (
            patch("telegram_share_bot.platform_previews._LOOKUP_TIMEOUT_SECONDS", 0.01),
            patch("telegram_share_bot.platform_previews._lookup_page_image", slow_lookup),
        ):
            self.assertIsNone(await resolve_preview("https://x.com/user/status/456"))


if __name__ == "__main__":
    unittest.main()
