"""Unit tests for TikTok photo-post slideshow support."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.downloader import DownloadError, MediaFormat, MediaKind
from telegram_share_bot.slideshow import (
    SlideshowSource,
    TikTokPhotoRef,
    _build_ffmpeg_argv,
    _cycle_images,
    build_slideshow_video,
    clear_short_link_cache,
    detect_tiktok_photo_post,
    download_tiktok_slideshow,
    extract_slideshow,
    plan_slideshow_timeline,
)


class TestDetectTikTokPhotoPost(unittest.TestCase):
    def tearDown(self) -> None:
        clear_short_link_cache()

    def test_matches_photo_url(self) -> None:
        ref = detect_tiktok_photo_post(
            "https://www.tiktok.com/@szia25.2/photo/7687274479570980128"
        )
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.user, "@szia25.2")
        self.assertEqual(ref.video_id, "7687274479570980128")
        self.assertEqual(
            ref.canonical_url,
            "https://www.tiktok.com/@szia25.2/photo/7687274479570980128",
        )

    def test_ignores_video_url(self) -> None:
        self.assertIsNone(
            detect_tiktok_photo_post(
                "https://www.tiktok.com/@user/video/7123456789012345678"
            )
        )

    def test_ignores_non_tiktok(self) -> None:
        self.assertIsNone(
            detect_tiktok_photo_post("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )

    def test_short_link_resolves_to_photo(self) -> None:
        photo = "https://www.tiktok.com/@szia25.2/photo/7687274479570980128"

        class FakeResponse:
            url = photo

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _n: int = -1) -> bytes:
                return b""

        fake_ydl = MagicMock()
        fake_ydl.urlopen.return_value = FakeResponse()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        with patch("telegram_share_bot.slideshow.yt_dlp.YoutubeDL", return_value=fake_ydl):
            ref = detect_tiktok_photo_post("https://vt.tiktok.com/ZSqcbj3bf/")
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.video_id, "7687274479570980128")

        # Second call should hit the memo cache (no extra urlopen).
        with patch("telegram_share_bot.slideshow.yt_dlp.YoutubeDL") as ydl_cls:
            ref2 = detect_tiktok_photo_post("https://vt.tiktok.com/ZSqcbj3bf/")
        ydl_cls.assert_not_called()
        self.assertEqual(ref2, ref)


class TestExtractSlideshow(unittest.TestCase):
    def test_parses_image_post_and_caps_images(self) -> None:
        images = [
            {
                "imageURL": {
                    "urlList": [f"https://cdn.example.com/img{i}.jpg"]
                }
            }
            for i in range(5)
        ]
        item = {
            "imagePost": {"images": images},
            "music": {
                "playUrl": "https://cdn.example.com/audio.mp3",
                "duration": 27,
            },
            "desc": "hello slideshow",
        }
        fake_ie = MagicMock()
        fake_ie._create_url.return_value = (
            "https://www.tiktok.com/@u/video/1"
        )
        fake_ie._extract_web_data_and_status.return_value = (item, 0)

        fake_ydl = MagicMock()
        fake_ydl.get_info_extractor.return_value = fake_ie
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        ref = TikTokPhotoRef(
            user="@u",
            video_id="1",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )
        with (
            patch("telegram_share_bot.slideshow.yt_dlp.YoutubeDL", return_value=fake_ydl),
            patch(
                "telegram_share_bot.slideshow.is_safe_media_url",
                return_value=True,
            ),
        ):
            source = extract_slideshow(ref, max_images=3)

        self.assertEqual(len(source.image_urls), 3)
        self.assertEqual(source.audio_url, "https://cdn.example.com/audio.mp3")
        self.assertEqual(source.audio_duration, 27.0)
        self.assertEqual(source.title, "hello slideshow")

    def test_no_images_raises(self) -> None:
        item = {"imagePost": {"images": []}, "desc": "empty"}
        fake_ie = MagicMock()
        fake_ie._create_url.return_value = "https://www.tiktok.com/@u/video/1"
        fake_ie._extract_web_data_and_status.return_value = (item, 0)
        fake_ydl = MagicMock()
        fake_ydl.get_info_extractor.return_value = fake_ie
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        ref = TikTokPhotoRef(
            user="@u",
            video_id="1",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )
        with patch(
            "telegram_share_bot.slideshow.yt_dlp.YoutubeDL", return_value=fake_ydl
        ):
            with self.assertRaises(DownloadError) as ctx:
                extract_slideshow(ref)
        self.assertEqual(str(ctx.exception), strings.SLIDESHOW_NO_IMAGES)


class TestPlanSlideshowTimeline(unittest.TestCase):
    def test_no_audio_uses_per_slide(self) -> None:
        total, durs = plan_slideshow_timeline(4, 2.5, None)
        self.assertEqual(total, 10.0)
        self.assertEqual(durs, [2.5, 2.5, 2.5, 2.5])

    def test_loops_images_to_cover_full_audio(self) -> None:
        # 1 image, 27s audio, 2.5s/slide -> 11 slots covering 27s
        total, durs = plan_slideshow_timeline(1, 2.5, 27.0)
        self.assertEqual(total, 27.0)
        self.assertEqual(len(durs), 11)
        self.assertAlmostEqual(sum(durs), 27.0, places=3)
        self.assertTrue(all(d == 2.5 for d in durs[:-1]))
        self.assertAlmostEqual(durs[-1], 2.0, places=3)

    def test_shrinks_slides_when_many_images(self) -> None:
        # 20 images * 2.5s = 50s > 27s audio -> fit all into 27s
        total, durs = plan_slideshow_timeline(20, 2.5, 27.0)
        self.assertEqual(total, 27.0)
        self.assertEqual(len(durs), 20)
        self.assertAlmostEqual(sum(durs), 27.0, places=5)
        self.assertAlmostEqual(durs[0], 27.0 / 20, places=5)

    def test_once_mode_trims_audio_after_one_pass(self) -> None:
        total, durs = plan_slideshow_timeline(
            4, 2.5, 27.0, images_loop=False
        )
        self.assertEqual(total, 10.0)
        self.assertEqual(durs, [2.5, 2.5, 2.5, 2.5])

    def test_once_mode_single_image_still_fits_full_audio(self) -> None:
        total, durs = plan_slideshow_timeline(
            1, 2.5, 27.0, images_loop=False
        )
        self.assertEqual(total, 27.0)
        self.assertEqual(len(durs), 11)
        self.assertAlmostEqual(sum(durs), 27.0, places=3)

    def test_cycle_images(self) -> None:
        paths = [Path("a.jpg"), Path("b.jpg")]
        cycled = _cycle_images(paths, 5)
        self.assertEqual(
            [p.name for p in cycled],
            ["a.jpg", "b.jpg", "a.jpg", "b.jpg", "a.jpg"],
        )


class TestProbeMediaDuration(unittest.TestCase):
    def test_prefers_mutagen(self) -> None:
        with (
            patch(
                "telegram_share_bot.slideshow._duration_from_mutagen",
                return_value=12.5,
            ) as mutagen_probe,
            patch(
                "telegram_share_bot.slideshow._duration_from_ffmpeg",
            ) as ffmpeg_probe,
        ):
            from telegram_share_bot.slideshow import _probe_media_duration

            result = _probe_media_duration(Path("audio.mp3"))
        self.assertEqual(result, 12.5)
        mutagen_probe.assert_called_once()
        ffmpeg_probe.assert_not_called()

    def test_falls_back_to_ffmpeg_stderr(self) -> None:
        completed = MagicMock()
        completed.stderr = (
            b"Input #0, mp3, from 'audio.mp3':\n"
            b"  Duration: 00:00:27.40, start: 0.000000, bitrate: 128 kb/s\n"
        )
        completed.returncode = 1

        with (
            patch(
                "telegram_share_bot.slideshow._duration_from_mutagen",
                return_value=None,
            ),
            patch("telegram_share_bot.slideshow.shutil.which", return_value="ffmpeg"),
            patch(
                "telegram_share_bot.slideshow.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            from telegram_share_bot.slideshow import _probe_media_duration

            result = _probe_media_duration(Path("audio.mp3"))
        self.assertAlmostEqual(result or 0.0, 27.4, places=2)
        run_mock.assert_called_once()
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("-i", argv)

    def test_returns_none_when_both_fail(self) -> None:
        with (
            patch(
                "telegram_share_bot.slideshow._duration_from_mutagen",
                return_value=None,
            ),
            patch(
                "telegram_share_bot.slideshow._duration_from_ffmpeg",
                return_value=None,
            ),
        ):
            from telegram_share_bot.slideshow import _probe_media_duration

            self.assertIsNone(_probe_media_duration(Path("missing.mp3")))


class TestBuildFfmpegArgv(unittest.TestCase):
    def test_argv_plays_audio_once_with_per_slot_durations(self) -> None:
        images = [Path(f"img{i}.jpg") for i in range(3)]
        audio = Path("audio.mp3")
        out = Path("out.mp4")
        argv = _build_ffmpeg_argv(
            ffmpeg_bin="ffmpeg",
            image_paths=images,
            audio_path=audio,
            output_path=out,
            slide_durations=[2.5, 2.5, 2.0],
            total=7.0,
            width=1080,
            height=1920,
            crf=24,
        )
        self.assertEqual(argv[0], "ffmpeg")
        self.assertNotIn("-stream_loop", argv)
        # Three image inputs + 1 audio
        self.assertEqual(argv.count("-i"), 4)
        self.assertEqual(argv.count("-loop"), 3)
        t_values = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-t"]
        self.assertIn("2.500", t_values)
        self.assertIn("2.000", t_values)
        self.assertIn("7.000", t_values)
        self.assertIn(str(out), argv)

    def test_argv_overlays_nav_dots_for_multi_image(self) -> None:
        images = [Path(f"img{i}.jpg") for i in range(3)]
        nav = [Path(f"nav_{i}.png") for i in range(3)]
        argv = _build_ffmpeg_argv(
            ffmpeg_bin="ffmpeg",
            image_paths=images,
            audio_path=None,
            output_path=Path("out.mp4"),
            slide_durations=[2.5, 2.5, 2.5],
            total=7.5,
            width=1080,
            height=1920,
            crf=24,
            unique_image_count=3,
            nav_overlay_paths=nav,
        )
        # 3 slides + 3 nav overlays
        self.assertEqual(argv.count("-i"), 6)
        fc = argv[argv.index("-filter_complex") + 1]
        self.assertIn("overlay=", fc)
        self.assertIn("shortest=1", fc)
        # Active nav index cycles with slot index
        self.assertIn("[b0][3:v]overlay", fc)
        self.assertIn("[b1][4:v]overlay", fc)
        self.assertIn("[b2][5:v]overlay", fc)

    def test_argv_skips_nav_for_single_image(self) -> None:
        argv = _build_ffmpeg_argv(
            ffmpeg_bin="ffmpeg",
            image_paths=[Path("img0.jpg")],
            audio_path=None,
            output_path=Path("out.mp4"),
            slide_durations=[2.5],
            total=2.5,
            width=1080,
            height=1920,
            crf=24,
            unique_image_count=1,
            nav_overlay_paths=[],
        )
        fc = argv[argv.index("-filter_complex") + 1]
        self.assertNotIn("overlay=", fc)


class TestNavDots(unittest.TestCase):
    def test_render_nav_dot_png(self) -> None:
        from telegram_share_bot.slideshow import render_nav_dot_png

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav.png"
            render_nav_dot_png(
                path,
                unique_count=5,
                active_index=2,
                frame_width=1080,
            )
            self.assertTrue(path.exists())
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(data), 100)


class TestBuildSlideshowVideo(unittest.TestCase):
    def test_missing_ffmpeg_raises(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/a.jpg",),
            audio_url=None,
            title="t",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("telegram_share_bot.slideshow.shutil.which", return_value=None):
                with self.assertRaises(DownloadError) as ctx:
                    build_slideshow_video(
                        source,
                        Path(tmp),
                        max_file_bytes=10_000_000,
                        timeout_seconds=30,
                    )
        self.assertEqual(str(ctx.exception), strings.SLIDESHOW_FFMPEG_MISSING)

    def test_output_over_source_bound_fails_after_one_render(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/a.jpg",),
            audio_url=None,
            title="t",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )

        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, n: int = -1) -> bytes:
                if not hasattr(self, "_done"):
                    self._done = True
                    return b"fake-image-bytes"
                return b""

        fake_ydl = MagicMock()
        fake_ydl.urlopen.return_value = FakeResponse()
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        run_calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> MagicMock:
            _ = kwargs
            run_calls.append(list(argv))
            # Write an oversized file at the output path (last argv element).
            out = Path(argv[-1])
            out.write_bytes(b"x" * 2000)
            result = MagicMock()
            result.returncode = 0
            result.stderr = b""
            return result

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with (
                patch(
                    "telegram_share_bot.slideshow.shutil.which",
                    return_value="ffmpeg",
                ),
                patch(
                    "telegram_share_bot.slideshow.yt_dlp.YoutubeDL",
                    return_value=fake_ydl,
                ),
                patch(
                    "telegram_share_bot.slideshow._safe_dns_resolution",
                ) as dns_cm,
                patch(
                    "telegram_share_bot.slideshow.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                dns_cm.return_value.__enter__ = MagicMock(return_value=None)
                dns_cm.return_value.__exit__ = MagicMock(return_value=False)
                with self.assertRaises(DownloadError) as ctx:
                    build_slideshow_video(
                        source,
                        work,
                        max_file_bytes=500,  # force both encode attempts to fail
                        timeout_seconds=30,
                    )

        self.assertIn("limit", str(ctx.exception).lower())
        self.assertEqual(len(run_calls), 1)
        # The shared downloader handles any over-limit output afterwards.
        self.assertIn("24", run_calls[0])

    def test_successful_build_returns_video(self) -> None:
        source = SlideshowSource(
            image_urls=(
                "https://cdn.example.com/a.jpg",
                "https://cdn.example.com/b.jpg",
            ),
            audio_url="https://cdn.example.com/a.mp3",
            title="two slides",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )

        payloads = {
            "https://cdn.example.com/a.jpg": b"img-a",
            "https://cdn.example.com/b.jpg": b"img-b",
            "https://cdn.example.com/a.mp3": b"audio-bytes",
        }

        class FakeResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._pos = 0

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, n: int = -1) -> bytes:
                if self._pos >= len(self._data):
                    return b""
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

        fake_ydl = MagicMock()

        def urlopen(request: Any) -> FakeResponse:
            url = getattr(request, "url", None) or str(request)
            return FakeResponse(payloads[url])

        fake_ydl.urlopen.side_effect = urlopen
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        def fake_run(argv: list[str], **kwargs: Any) -> MagicMock:
            _ = kwargs
            Path(argv[-1]).write_bytes(b"mp4-content-ok")
            result = MagicMock()
            result.returncode = 0
            result.stderr = b""
            return result

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with (
                patch(
                    "telegram_share_bot.slideshow.shutil.which",
                    return_value="ffmpeg",
                ),
                patch(
                    "telegram_share_bot.slideshow.yt_dlp.YoutubeDL",
                    return_value=fake_ydl,
                ),
                patch(
                    "telegram_share_bot.slideshow._safe_dns_resolution",
                ) as dns_cm,
                patch(
                    "telegram_share_bot.slideshow.subprocess.run",
                    side_effect=fake_run,
                ),
                patch(
                    "telegram_share_bot.slideshow._probe_media_duration",
                    return_value=None,
                ),
            ):
                dns_cm.return_value.__enter__ = MagicMock(return_value=None)
                dns_cm.return_value.__exit__ = MagicMock(return_value=False)
                media = build_slideshow_video(
                    source,
                    work,
                    max_file_bytes=10_000_000,
                    timeout_seconds=30,
                    slide_ms=2500,
                )
                self.assertEqual(media.kind, MediaKind.VIDEO)
                self.assertEqual(media.title, "two slides")
                # No usable audio duration -> 2 * 2.5s
                self.assertEqual(media.duration, 5)
                self.assertTrue(media.path.exists())

    def test_audio_duration_loops_single_image(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/a.jpg",),
            audio_url="https://cdn.example.com/a.mp3",
            title="one slide",
            canonical_url="https://www.tiktok.com/@u/photo/1",
            audio_duration=10.0,
        )

        class FakeResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._pos = 0

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, n: int = -1) -> bytes:
                if self._pos >= len(self._data):
                    return b""
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

        payloads = {
            "https://cdn.example.com/a.jpg": b"img-a",
            "https://cdn.example.com/a.mp3": b"audio-bytes",
        }
        fake_ydl = MagicMock()

        def urlopen(request: Any) -> FakeResponse:
            url = getattr(request, "url", None) or str(request)
            return FakeResponse(payloads[url])

        fake_ydl.urlopen.side_effect = urlopen
        fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
        fake_ydl.__exit__ = MagicMock(return_value=False)

        captured: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> MagicMock:
            _ = kwargs
            captured.append(list(argv))
            Path(argv[-1]).write_bytes(b"mp4-ok")
            result = MagicMock()
            result.returncode = 0
            result.stderr = b""
            return result

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with (
                patch(
                    "telegram_share_bot.slideshow.shutil.which",
                    return_value="ffmpeg",
                ),
                patch(
                    "telegram_share_bot.slideshow.yt_dlp.YoutubeDL",
                    return_value=fake_ydl,
                ),
                patch(
                    "telegram_share_bot.slideshow._safe_dns_resolution",
                ) as dns_cm,
                patch(
                    "telegram_share_bot.slideshow.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                dns_cm.return_value.__enter__ = MagicMock(return_value=None)
                dns_cm.return_value.__exit__ = MagicMock(return_value=False)
                media = build_slideshow_video(
                    source,
                    work,
                    max_file_bytes=10_000_000,
                    timeout_seconds=30,
                    slide_ms=2500,
                )
                self.assertEqual(media.duration, 10)
                # 10s / 2.5s = 4 cycled image inputs + 1 audio
                self.assertEqual(captured[0].count("-loop"), 4)
                self.assertEqual(captured[0].count("-i"), 5)
                self.assertNotIn("-stream_loop", captured[0])
                self.assertIn("10.000", captured[0])


class TestSlideshowAudioChoice(unittest.TestCase):
    def test_audio_choice_downloads_the_slideshow_soundtrack(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/slide.jpg",),
            audio_url="https://cdn.example.com/audio.mp3",
            title="Photo post",
            canonical_url="https://www.tiktok.com/@u/photo/1",
            audio_duration=12.0,
        )
        ref = TikTokPhotoRef(
            user="@u", video_id="1", canonical_url=source.canonical_url
        )

        def save_audio(
            _ydl: Any, _url: str, path: Path, *, remaining_budget: int, abort_event: Any
        ) -> int:
            _ = remaining_budget, abort_event
            path.write_bytes(b"soundtrack")
            return path.stat().st_size

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.slideshow.detect_tiktok_photo_post", return_value=ref),
                patch("telegram_share_bot.slideshow.extract_slideshow", return_value=source),
                patch("telegram_share_bot.slideshow._safe_dns_resolution") as dns_cm,
                patch("telegram_share_bot.slideshow._download_bytes", side_effect=save_audio),
            ):
                dns_cm.return_value.__enter__ = MagicMock(return_value=None)
                dns_cm.return_value.__exit__ = MagicMock(return_value=False)
                media = download_tiktok_slideshow(
                    source.canonical_url,
                    Path(tmp),
                    max_file_bytes=1024,
                    timeout_seconds=10,
                    media_format=MediaFormat.AUDIO,
                )

        self.assertIsNotNone(media)
        assert media is not None
        self.assertEqual(media.kind, MediaKind.AUDIO)
        self.assertEqual(media.path.name, "slideshow-audio.mp3")
        self.assertEqual(media.duration, 12)

    def test_long_slideshow_soundtrack_is_rejected_before_asset_download(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/slide.jpg",),
            audio_url="https://cdn.example.com/sound.mp3",
            title="Long photo post",
            canonical_url="https://www.tiktok.com/@u/photo/1",
            audio_duration=3600,
        )
        ref = TikTokPhotoRef(
            user="@u", video_id="1", canonical_url=source.canonical_url
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.slideshow.detect_tiktok_photo_post", return_value=ref),
                patch("telegram_share_bot.slideshow.extract_slideshow", return_value=source),
                patch("telegram_share_bot.slideshow._download_bytes") as download_bytes,
            ):
                with self.assertRaises(DownloadError) as ctx:
                    download_tiktok_slideshow(
                        source.canonical_url,
                        Path(tmp),
                        max_file_bytes=1024,
                        timeout_seconds=10,
                    )
                download_bytes.assert_not_called()
        self.assertIn("30 min", str(ctx.exception))

    def test_audio_choice_reports_missing_slideshow_soundtrack(self) -> None:
        source = SlideshowSource(
            image_urls=("https://cdn.example.com/slide.jpg",),
            audio_url=None,
            title="Silent photo post",
            canonical_url="https://www.tiktok.com/@u/photo/1",
        )
        ref = TikTokPhotoRef(
            user="@u", video_id="1", canonical_url=source.canonical_url
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.slideshow.detect_tiktok_photo_post", return_value=ref),
                patch("telegram_share_bot.slideshow.extract_slideshow", return_value=source),
            ):
                with self.assertRaises(DownloadError) as ctx:
                    download_tiktok_slideshow(
                        source.canonical_url,
                        Path(tmp),
                        max_file_bytes=1024,
                        timeout_seconds=10,
                        media_format=MediaFormat.AUDIO,
                    )
        self.assertEqual(str(ctx.exception), strings.AUDIO_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
