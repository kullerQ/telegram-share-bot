"""Tests for YouTube clip time-range parsing, cache keys, and section download."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot.cache import MediaCache, _cache_key
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import (
    MAX_CLIP_SECONDS,
    DownloadError,
    MediaKind,
    TimeRange,
    _clamp_time_range,
    _download_sync,
    extract_media_request,
    format_time_range,
    parse_duration_seconds_token,
    parse_time_range_token,
    parse_youtube_start_seconds,
)
from telegram_share_bot.handlers import inline_query
from telegram_share_bot.normalizer import is_youtube_url


class TestParseTimeRangeToken(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(parse_time_range_token("90-150"), TimeRange(90, 150))

    def test_m_ss(self) -> None:
        self.assertEqual(parse_time_range_token("1:20-2:05"), TimeRange(80, 125))

    def test_m_ss_large_minutes(self) -> None:
        self.assertEqual(parse_time_range_token("90:12-91:00"), TimeRange(5412, 5460))

    def test_h_mm_ss(self) -> None:
        self.assertEqual(
            parse_time_range_token("1:02:03-1:05:00"), TimeRange(3723, 3900)
        )

    def test_mixed(self) -> None:
        self.assertEqual(parse_time_range_token("90-2:05"), TimeRange(90, 125))

    def test_end_before_start(self) -> None:
        self.assertIsNone(parse_time_range_token("2:05-1:20"))

    def test_end_equal_start(self) -> None:
        self.assertIsNone(parse_time_range_token("80-80"))

    def test_invalid_hms_minutes(self) -> None:
        self.assertIsNone(parse_time_range_token("1:99:00-2:00:00"))

    def test_not_a_range(self) -> None:
        self.assertIsNone(parse_time_range_token("hello"))
        self.assertIsNone(parse_time_range_token("1:20"))

    def test_format_label(self) -> None:
        self.assertEqual(format_time_range(TimeRange(80, 125)), "1:20-2:05")
        self.assertEqual(format_time_range(TimeRange(3723, 3900)), "1:02:03-1:05:00")
        self.assertEqual(format_time_range(TimeRange(2022, None)), "33:42-end")


class TestClampOpenEnded(unittest.TestCase):
    def test_clamp_open_ended_to_duration(self) -> None:
        resolved = _clamp_time_range(
            TimeRange(100, None), {"duration": 250}
        )
        self.assertEqual(resolved, TimeRange(100, 250))

    def test_clamp_open_ended_over_max_checked_by_resolve(self) -> None:
        from telegram_share_bot.downloader import _resolve_clip_range

        with self.assertRaises(DownloadError):
            _resolve_clip_range(
                TimeRange(0, None), {"duration": MAX_CLIP_SECONDS + 100}
            )


class TestExtractMediaRequest(unittest.TestCase):
    def test_youtube_with_range(self) -> None:
        req = extract_media_request(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ 1:20-2:05"
        )
        self.assertIsNotNone(req.url)
        self.assertEqual(req.time_range, TimeRange(80, 125))
        self.assertIsNone(req.custom_caption)

    def test_youtube_range_then_caption(self) -> None:
        req = extract_media_request(
            "https://youtu.be/dQw4w9WgXcQ 1:20-2:05 my caption"
        )
        self.assertEqual(req.time_range, TimeRange(80, 125))
        self.assertEqual(req.custom_caption, "my caption")

    def test_youtube_caption_without_range(self) -> None:
        req = extract_media_request(
            "https://youtube.com/watch?v=dQw4w9WgXcQ my caption here"
        )
        self.assertIsNone(req.time_range)
        self.assertEqual(req.custom_caption, "my caption here")

    def test_range_not_first_token(self) -> None:
        req = extract_media_request(
            "https://youtube.com/watch?v=dQw4w9WgXcQ hello 1:20-2:05"
        )
        self.assertIsNone(req.time_range)
        self.assertEqual(req.custom_caption, "hello 1:20-2:05")

    def test_tiktok_range_stays_caption(self) -> None:
        req = extract_media_request(
            "https://www.tiktok.com/@user/video/1234567890123456789 1:20-2:05 hello"
        )
        self.assertIsNone(req.time_range)
        self.assertEqual(req.custom_caption, "1:20-2:05 hello")

    def test_youtube_url_only(self) -> None:
        req = extract_media_request("https://youtu.be/dQw4w9WgXcQ")
        self.assertIsNone(req.time_range)
        self.assertIsNone(req.custom_caption)

    def test_youtube_t_plus_duration(self) -> None:
        req = extract_media_request("https://youtu.be/-gLCzX0WlpY?t=2022 30")
        self.assertEqual(req.time_range, TimeRange(2022, 2052))
        self.assertIsNone(req.custom_caption)

    def test_youtube_t_plus_duration_then_caption(self) -> None:
        req = extract_media_request(
            "https://youtu.be/-gLCzX0WlpY?t=2022 30 optional caption"
        )
        self.assertEqual(req.time_range, TimeRange(2022, 2052))
        self.assertEqual(req.custom_caption, "optional caption")

    def test_youtube_t_alone_open_ended(self) -> None:
        req = extract_media_request("https://youtu.be/-gLCzX0WlpY?t=2022")
        self.assertEqual(req.time_range, TimeRange(2022, None))
        self.assertIsNone(req.custom_caption)

    def test_youtube_t_alone_with_caption(self) -> None:
        req = extract_media_request(
            "https://youtu.be/-gLCzX0WlpY?t=2022 hello world"
        )
        self.assertEqual(req.time_range, TimeRange(2022, None))
        self.assertEqual(req.custom_caption, "hello world")

    def test_youtube_duration_without_t_is_caption(self) -> None:
        req = extract_media_request(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ 30"
        )
        self.assertIsNone(req.time_range)
        self.assertEqual(req.custom_caption, "30")

    def test_absolute_range_preferred_over_t(self) -> None:
        req = extract_media_request(
            "https://youtu.be/-gLCzX0WlpY?t=2022 1:20-2:05"
        )
        self.assertEqual(req.time_range, TimeRange(80, 125))

    def test_youtube_clock_t_plus_duration(self) -> None:
        req = extract_media_request(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s 45"
        )
        self.assertEqual(req.time_range, TimeRange(3723, 3768))


class TestYoutubeStartAndDuration(unittest.TestCase):
    def test_parse_start_plain_seconds(self) -> None:
        self.assertEqual(
            parse_youtube_start_seconds("https://youtu.be/abc?t=2022"),
            2022,
        )

    def test_parse_start_clock(self) -> None:
        self.assertEqual(
            parse_youtube_start_seconds(
                "https://www.youtube.com/watch?v=abc&t=33m42s"
            ),
            2022,
        )
        self.assertEqual(
            parse_youtube_start_seconds(
                "https://www.youtube.com/watch?v=abc&t=1h2m3s"
            ),
            3723,
        )

    def test_parse_start_param(self) -> None:
        self.assertEqual(
            parse_youtube_start_seconds(
                "https://www.youtube.com/watch?v=abc&start=90"
            ),
            90,
        )

    def test_parse_start_fragment(self) -> None:
        self.assertEqual(
            parse_youtube_start_seconds("https://youtu.be/abc#t=120"),
            120,
        )

    def test_parse_start_missing(self) -> None:
        self.assertIsNone(
            parse_youtube_start_seconds("https://youtu.be/abc")
        )

    def test_duration_token(self) -> None:
        self.assertEqual(parse_duration_seconds_token("30"), 30)
        self.assertIsNone(parse_duration_seconds_token("0"))
        self.assertIsNone(parse_duration_seconds_token("1:30"))
        self.assertIsNone(parse_duration_seconds_token("30s"))


class TestIsYoutubeUrl(unittest.TestCase):
    def test_hosts(self) -> None:
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(is_youtube_url("https://m.youtube.com/watch?v=abc"))
        self.assertFalse(is_youtube_url("https://www.tiktok.com/@u/video/1"))


class TestCacheKeyWithRange(unittest.IsolatedAsyncioTestCase):
    def test_keys_differ(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        full = _cache_key(url, None)
        clip = _cache_key(url, TimeRange(80, 125))
        open_ended = _cache_key(url, TimeRange(80, None))
        self.assertNotEqual(full, clip)
        self.assertNotEqual(clip, open_ended)
        assert full is not None and clip is not None and open_ended is not None
        self.assertTrue(clip.startswith(full))
        self.assertIn("#t=80-125", clip)
        self.assertIn("#t=80-end", open_ended)

    async def test_cache_roundtrip_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = MediaCache(Path(tmp) / "cache.db")
            url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            range_ = TimeRange(10, 20)
            await cache.set(
                url, "fid-full", MediaKind.VIDEO, "Full", 100, time_range=None
            )
            await cache.set(
                url, "fid-clip", MediaKind.VIDEO, "Clip", 10, time_range=range_
            )
            full = await cache.get(url, time_range=None)
            clip = await cache.get(url, time_range=range_)
            assert full is not None and clip is not None
            self.assertEqual(full.file_id, "fid-full")
            self.assertEqual(clip.file_id, "fid-clip")


class TestInlineClipChoice(unittest.IsolatedAsyncioTestCase):
    def _context(self) -> MagicMock:
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp = tmp
        # Keep cache DB path for the test instance
        tmp_path = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_path, ignore_errors=True))
        context = MagicMock()
        context.application.bot_data = {
            "settings": Settings(
                bot_token="test:token",
                storage_chat_id=1,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=tmp_path,
                cache_db_path=tmp_path / "cache.db",
                delete_storage_messages=False,
                allow_public=True,
            ),
            "media_cache": MediaCache(tmp_path / "cache.db"),
        }
        return context

    async def test_youtube_range_offers_two_results(self) -> None:
        context = self._context()
        update = MagicMock()
        query = MagicMock()
        query.from_user = MagicMock(id=1)
        query.query = "https://www.youtube.com/watch?v=dQw4w9WgXcQ 1:20-2:05"
        query.answer = AsyncMock()
        update.inline_query = query

        await inline_query(update, context)
        query.answer.assert_awaited()
        kwargs = query.answer.await_args.kwargs
        results = kwargs["results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].id.startswith("clip:"))
        self.assertTrue(results[1].id.startswith("full:"))
        self.assertIn("clip", results[0].title.lower())
        self.assertIn("full", results[1].title.lower())

    async def test_youtube_t_alone_offers_two_results(self) -> None:
        context = self._context()
        update = MagicMock()
        query = MagicMock()
        query.from_user = MagicMock(id=1)
        query.query = "https://youtu.be/-gLCzX0WlpY?t=2022"
        query.answer = AsyncMock()
        update.inline_query = query

        await inline_query(update, context)
        results = query.answer.await_args.kwargs["results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].id.startswith("clip:"))
        self.assertTrue(results[1].id.startswith("full:"))
        self.assertIn("end", results[0].title.lower())

    async def test_youtube_caption_without_range_one_result(self) -> None:
        context = self._context()
        update = MagicMock()
        query = MagicMock()
        query.from_user = MagicMock(id=1)
        query.query = "https://www.youtube.com/watch?v=dQw4w9WgXcQ my caption here"
        query.answer = AsyncMock()
        update.inline_query = query

        await inline_query(update, context)
        results = query.answer.await_args.kwargs["results"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].id.startswith("clip:"))

    async def test_tiktok_range_token_one_result(self) -> None:
        context = self._context()
        update = MagicMock()
        query = MagicMock()
        query.from_user = MagicMock(id=1)
        query.query = (
            "https://www.tiktok.com/@user/video/1234567890123456789 1:20-2:05 hello"
        )
        query.answer = AsyncMock()
        update.inline_query = query

        with patch(
            "telegram_share_bot.handlers.platform_previews.resolve_preview",
            new=AsyncMock(return_value=None),
        ):
            await inline_query(update, context)
        results = query.answer.await_args.kwargs["results"]
        self.assertEqual(len(results), 1)


class TestClipDownloadOpts(unittest.TestCase):
    def test_section_download_sets_ranges_omits_max_filesize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = Path(tmp)
            fake_path = download_dir / "out.mp4"
            fake_path.write_bytes(b"x" * 100)
            params_holder: list[dict[str, object]] = []

            class FakeYdl:
                def __init__(self, opts: dict[str, object]) -> None:
                    self.params: dict[str, object] = dict(opts)

                def __enter__(self) -> FakeYdl:
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

                def process_ie_result(
                    self, info: dict[str, object], download: bool = True
                ) -> dict[str, object]:
                    params_holder.append(dict(self.params))
                    return info

                def prepare_filename(self, info: dict[str, object]) -> str:
                    return str(fake_path)

            with (
                patch(
                    "telegram_share_bot.downloader.yt_dlp.YoutubeDL",
                    FakeYdl,
                ),
                patch(
                    "telegram_share_bot.downloader.shutil.which",
                    return_value="/usr/bin/ffmpeg",
                ),
                patch(
                    "telegram_share_bot.downloader._safe_dns_resolution",
                    MagicMock(
                        return_value=MagicMock(
                            __enter__=MagicMock(return_value=None),
                            __exit__=MagicMock(return_value=None),
                        )
                    ),
                ),
                patch(
                    "telegram_share_bot.downloader._resolve_downloaded_path",
                    return_value=fake_path,
                ),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    side_effect=lambda ydl, url, force_refresh=False: (
                        {
                            "id": "abc",
                            "title": "Test",
                            "duration": 600,
                            "ext": "mp4",
                        },
                        False,
                    ),
                ),
            ):
                media = _download_sync(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    download_dir,
                    max_file_bytes=45 * 1024 * 1024,
                    timeout_seconds=90,
                    time_range=TimeRange(80, 125),
                )
                self.assertEqual(media.duration, 45)
                self.assertTrue(params_holder)
                self.assertIn("download_ranges", params_holder[0])
                self.assertNotIn("max_filesize", params_holder[0])
                self.assertEqual(params_holder[0].get("format"), "bv*+ba/b")
                self.assertNotIn("height<=1080", str(params_holder[0].get("format")))

    def test_clip_too_long_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "telegram_share_bot.downloader.shutil.which",
                return_value="/usr/bin/ffmpeg",
            ):
                with self.assertRaises(DownloadError) as ctx:
                    _download_sync(
                        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        Path(tmp),
                        max_file_bytes=45 * 1024 * 1024,
                        timeout_seconds=90,
                        time_range=TimeRange(0, MAX_CLIP_SECONDS + 1),
                    )
                self.assertIn("minutes", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
