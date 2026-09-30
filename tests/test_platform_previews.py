"""Checks for bounded platform thumbnails and safe logo fallback."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from telegram_share_bot.media.models import DownloadError
from telegram_share_bot.media.work import MediaWorkSupervisor
from telegram_share_bot.platforms.previews import (
    Preview,
    PreviewResolver,
    _lookup_page_image,
    _lookup_reddit,
    _lookup_tiktok,
    _lookup_tiktok_media,
    _trusted_image_url,
    resolve_preview,
)
from telegram_share_bot.tiktok.source import SlideshowSource


class TestPlatformPreviews(unittest.IsolatedAsyncioTestCase):
    async def _resolver(
        self,
        handler: object | None = None,
        *,
        max_active_lookups: int = 4,
        photo_workers: int = 2,
    ) -> PreviewResolver:
        transport = httpx.MockTransport(handler or (lambda _request: httpx.Response(200)))
        client = httpx.AsyncClient(transport=transport)  # type: ignore[arg-type]
        resolver = PreviewResolver(
            max_active_lookups=max_active_lookups,
            photo_workers=photo_workers,
        )
        await resolver.start(client)
        self.addAsyncCleanup(resolver.close)
        return resolver

    async def test_youtube_preview_needs_no_http_request(self) -> None:
        with patch("telegram_share_bot.platforms.previews.httpx.AsyncClient") as client:
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

    async def test_tiktok_photo_uses_first_slideshow_image(self) -> None:
        url = "https://www.tiktok.com/@user/photo/123456789"
        image = "https://p16-common-sign.tiktokcdn-eu.com/first.jpeg"
        source = SlideshowSource((image,), None, "post", url)
        with patch(
            "telegram_share_bot.platforms.previews.extract_slideshow", return_value=source
        ) as extract:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
                preview = await _lookup_tiktok_media(url, client, MediaWorkSupervisor(2))
        self.assertEqual(preview, Preview(image))
        self.assertEqual(extract.call_args.args[0].canonical_url, url)
        self.assertEqual(
            extract.call_args.kwargs,
            {"max_images": 1, "https_only": True, "socket_timeout": 3},
        )

    async def test_tiktok_short_photo_link_uses_first_slideshow_image(self) -> None:
        photo_id = "7687699407227079966"
        canonical = f"https://www.tiktok.com/@rem0ri/photo/{photo_id}"
        image = "https://p16-common-sign.tiktokcdn-eu.com/first.jpeg"
        requests: list[str] = []

        def response(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                301,
                headers={
                    "location": "https://www.tiktok.com/@rem0ri/photo/"
                    f"{photo_id}?_r=1"
                },
            )

        source = SlideshowSource((image,), None, "post", canonical)
        with patch(
            "telegram_share_bot.platforms.previews.extract_slideshow", return_value=source
        ) as extract:
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                preview = await _lookup_tiktok_media(
                    "https://vt.tiktok.com/ZSqwHG2TG", client, MediaWorkSupervisor(2)
                )
        self.assertEqual(preview, Preview(image))
        self.assertEqual(requests, ["https://vt.tiktok.com/ZSqwHG2TG"])
        self.assertEqual(extract.call_args.args[0].canonical_url, canonical)

    async def test_tiktok_photo_uses_logo_when_first_image_is_unavailable(self) -> None:
        url = "https://www.tiktok.com/@user/photo/123456789"
        for result in (
            SlideshowSource(("https://untrusted.example/first.jpeg",), None, "post", url),
            DownloadError("no images"),
        ):
            with self.subTest(result=result), patch(
                "telegram_share_bot.platforms.previews.extract_slideshow",
                side_effect=result if isinstance(result, Exception) else None,
                return_value=None if isinstance(result, Exception) else result,
            ):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: None)
                ) as client:
                    self.assertIsNone(
                        await _lookup_tiktok_media(url, client, MediaWorkSupervisor(2))
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
        resolver = await self._resolver()
        with patch(
            "telegram_share_bot.platforms.previews._lookup_page_image",
            new=AsyncMock(return_value=expected),
        ) as lookup:
            preview = await resolve_preview(
                "https://x.com/PunchingCat/status/2103311089614340120?s=20", resolver
            )
        self.assertEqual(preview, expected)
        self.assertEqual(
            lookup.await_args.args[:2],
            ("https://x.com/PunchingCat/status/2103311089614340120", "x"),
        )

    async def test_x_video_path_uses_canonical_post_for_preview(self) -> None:
        expected = Preview("https://pbs.twimg.com/amplify_video_thumb/example.jpg")
        resolver = await self._resolver()
        with patch(
            "telegram_share_bot.platforms.previews._lookup_page_image",
            new=AsyncMock(return_value=expected),
        ) as lookup:
            preview = await resolve_preview(
                "https://x.com/animalsbabyy/status/2103205625752953328/video/1", resolver
            )
        self.assertEqual(preview, expected)
        self.assertEqual(
            lookup.await_args.args[:2],
            ("https://x.com/animalsbabyy/status/2103205625752953328", "x"),
        )

    async def test_x_media_previews_use_public_post_media_for_video_and_gif(self) -> None:
        thumbnails = {
            "2103205625752953328": (
                "https://pbs.twimg.com/amplify_video_thumb/video/cover.jpg?name=orig",
                640,
                360,
            ),
            "2105088370078531813": (
                "https://pbs.twimg.com/tweet_video_thumb/gif.jpg?name=orig",
                480,
                270,
            ),
        }

        def response(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "cdn.syndication.twimg.com")
            status_id = request.url.params["id"]
            image, width, height = thumbnails[status_id]
            return httpx.Response(
                200,
                json={
                    "mediaDetails": [
                        {
                            "type": "animated_gif" if "gif" in image else "video",
                            "media_url_https": image,
                            "sizes": {"medium": {"w": width, "h": height}},
                        }
                    ]
                },
            )

        resolver = await self._resolver(response)
        for url, status_id, width, height in (
            (
                "https://x.com/animalsbabyy/status/2103205625752953328/video/1",
                "2103205625752953328",
                640,
                360,
            ),
            (
                "https://x.com/i/status/2105088370078531813",
                "2105088370078531813",
                480,
                270,
            ),
        ):
            with self.subTest(status_id=status_id):
                preview = await resolve_preview(url, resolver)
                self.assertEqual(
                    preview,
                    Preview(
                        thumbnails[status_id][0].split("?", maxsplit=1)[0]
                        + "?format=jpg&name=small",
                        width,
                        height,
                    ),
                )

    async def test_x_syndication_rejects_untrusted_media_thumbnail(self) -> None:
        resolver = await self._resolver(
            lambda _request: httpx.Response(
                200,
                json={
                    "mediaDetails": [
                        {"media_url_https": "https://evil.example/thumbnail.jpg"}
                    ]
                },
            )
        )
        with patch(
            "telegram_share_bot.platforms.previews._lookup_page_image",
            new=AsyncMock(return_value=None),
        ) as page_lookup:
            preview = await resolve_preview("https://x.com/user/status/123", resolver)
        self.assertIsNone(preview)
        page_lookup.assert_awaited_once()

    async def test_failed_lookup_is_cached_as_logo_fallback(self) -> None:
        resolver = await self._resolver()
        with patch(
            "telegram_share_bot.platforms.previews._lookup_page_image",
            new=AsyncMock(return_value=None),
        ) as lookup:
            for _ in range(2):
                self.assertIsNone(
                    await resolve_preview("https://x.com/user/status/123", resolver)
                )
        lookup.assert_awaited_once()

    async def test_slow_lookup_returns_logo_fallback(self) -> None:
        resolver = await self._resolver()

        async def slow_lookup(*args: object) -> Preview | None:
            await asyncio.sleep(1)
            return Preview("https://pbs.twimg.com/late.jpg")

        with (
            patch("telegram_share_bot.platforms.previews._LOOKUP_TIMEOUT_SECONDS", 0.01),
            patch("telegram_share_bot.platforms.previews._lookup_page_image", slow_lookup),
        ):
            self.assertIsNone(await resolve_preview("https://x.com/user/status/456", resolver))

    async def test_tiktok_oembed_rejects_oversized_response_body(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"{" + b" " * (256 * 1024))
            )
        )
        async with client:
            preview = await _lookup_tiktok(
                "https://www.tiktok.com/@user/video/123456789", client
            )
        self.assertIsNone(preview)

    async def test_unique_lookups_never_exceed_request_capacity(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        active = 0
        peak = 0
        request_count = 0

        async def response(_request: httpx.Request) -> httpx.Response:
            nonlocal active, peak, request_count
            active += 1
            request_count += 1
            peak = max(peak, active)
            started.set()
            try:
                await gate.wait()
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<head><meta property='og:image' "
                    "content='https://pbs.twimg.com/preview.jpg'></head>",
                )
            finally:
                active -= 1

        resolver = await self._resolver(response, max_active_lookups=2)
        tasks = [
            asyncio.create_task(
                resolve_preview(f"https://x.com/user/status/{index}", resolver)
            )
            for index in range(8)
        ]
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertEqual(request_count, 2)
        self.assertLessEqual(peak, 2)
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(sum(result is not None for result in results), 2)

    async def test_identical_lookups_are_shared_and_waiter_cancel_isolated(self) -> None:
        started = asyncio.Event()
        gate = asyncio.Event()
        expected = Preview("https://pbs.twimg.com/shared.jpg")
        resolver = await self._resolver()

        async def slow_lookup(*_args: object) -> Preview:
            started.set()
            await gate.wait()
            return expected

        with patch(
            "telegram_share_bot.platforms.previews._lookup_page_image",
            new=AsyncMock(side_effect=slow_lookup),
        ) as lookup:
            first = asyncio.create_task(
                resolve_preview("https://x.com/user/status/789", resolver)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            second = asyncio.create_task(
                resolve_preview("https://x.com/user/status/789", resolver)
            )
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            gate.set()
            self.assertEqual(await second, expected)
        lookup.assert_awaited_once()

    async def test_close_cancels_shared_lookup_and_closes_client(self) -> None:
        started = asyncio.Event()

        async def response(_request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            return httpx.Response(200)

        resolver = await self._resolver(response)
        assert resolver._client is not None
        client = resolver._client
        caller = asyncio.create_task(
            resolve_preview("https://x.com/user/status/987", resolver)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await resolver.close()

        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(client.is_closed)

    async def test_timed_out_photo_worker_keeps_its_native_slot(self) -> None:
        resolver = await self._resolver(photo_workers=1)
        worker_started = threading.Event()
        worker_finish = threading.Event()
        url = "https://www.tiktok.com/@user/photo/123456789"
        source = SlideshowSource(
            ("https://p16-common-sign.tiktokcdn-eu.com/first.jpeg",), None, "post", url
        )

        def blocking_extract(*_args: object, **_kwargs: object) -> SlideshowSource:
            worker_started.set()
            worker_finish.wait(2)
            return source

        try:
            with (
                patch(
                    "telegram_share_bot.platforms.previews._TIKTOK_LOOKUP_TIMEOUT_SECONDS",
                    0.03,
                ),
                patch(
                    "telegram_share_bot.platforms.previews.extract_slideshow",
                    side_effect=blocking_extract,
                ),
            ):
                result = await resolve_preview(url, resolver)
            self.assertIsNone(result)
            self.assertTrue(worker_started.is_set())
            self.assertIsNone(await resolver._photo_work.try_acquire(1))
        finally:
            worker_finish.set()

        for _ in range(100):
            lease = await resolver._photo_work.try_acquire(1)
            if lease is not None:
                await lease.release()
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("native preview capacity was not restored after its worker exited")


if __name__ == "__main__":
    unittest.main()
