"""Adaptive Telegram-size video selection and optimization tests."""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yt_dlp

from telegram_share_bot import strings
from telegram_share_bot.downloader import (
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
    VideoQualityPolicy,
    _audio_format_candidates,
    _download_sync,
    _format_candidates,
    _optimize_video_file,
    _probe_video_file,
    _projected_transfer_seconds,
    _run_bounded_clip_ffmpeg,
    _set_attempt_format_selector,
    _set_attempt_output_template,
    _VideoProbe,
    _youtube_hls_clip_streams,
)


def _outtmpl_template(params: dict[str, object]) -> str:
    template = params["outtmpl"]
    if isinstance(template, dict):
        template = template["default"]
    return str(template)


class TestFormatRanking(unittest.TestCase):
    def test_auto_starts_with_highest_bounded_quality(self) -> None:
        formats = [
            {
                "format_id": "v1080",
                "height": 1080,
                "fps": 30,
                "tbr": 1000,
                "filesize": 55,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "v720",
                "height": 720,
                "fps": 30,
                "tbr": 700,
                "filesize": 38,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "v480",
                "height": 480,
                "fps": 30,
                "tbr": 400,
                "filesize": 18,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "audio",
                "abr": 128,
                "filesize": 2,
                "vcodec": "none",
                "acodec": "aac",
            },
        ]
        candidates = _format_candidates({"formats": formats}, max_file_bytes=45)
        self.assertEqual(
            [candidate.selector for candidate in candidates],
            ["v1080+audio", "v720+audio", "v480+audio"],
        )

    def test_balanced_prefers_progressive_tier_and_60_fps_avc(self) -> None:
        formats = [
            {"format_id": "v1080", "height": 1080, "fps": 60, "vcodec": "avc1", "acodec": "aac"},
            {"format_id": "v720-av1", "height": 720, "fps": 60, "vcodec": "av01", "acodec": "aac"},
            {"format_id": "v720-avc", "height": 720, "fps": 60, "vcodec": "avc1", "acodec": "aac"},
            {"format_id": "v480", "height": 480, "fps": 60, "vcodec": "avc1", "acodec": "aac"},
        ]
        candidates = _format_candidates(
            {"formats": formats},
            max_file_bytes=45 * 1024 * 1024,
            quality_policy=VideoQualityPolicy.BALANCED,
        )
        self.assertEqual(candidates[0].selector, "v720-avc")

    def test_balanced_falls_back_to_highest_available_quality(self) -> None:
        formats = [
            {
                "format_id": "v480",
                "height": 480,
                "fps": 30,
                "filesize": 10,
                "vcodec": "avc1",
                "acodec": "aac",
            },
            {
                "format_id": "v1080",
                "height": 1080,
                "fps": 30,
                "filesize": 30,
                "vcodec": "vp9",
                "acodec": "aac",
            },
        ]
        candidates = _format_candidates(
            {"formats": formats},
            max_file_bytes=45 * 1024 * 1024,
            quality_policy=VideoQualityPolicy.BALANCED,
        )
        self.assertEqual(candidates[0].selector, "v1080")

    def test_best_keeps_top_source_without_lower_quality_fallback(self) -> None:
        formats = [
            {
                "format_id": "v1440",
                "height": 1440,
                "fps": 60,
                "filesize": 200,
                "vcodec": "avc1",
                "acodec": "aac",
            },
            {
                "format_id": "v720",
                "height": 720,
                "fps": 30,
                "filesize": 40,
                "vcodec": "avc1",
                "acodec": "aac",
            },
        ]
        candidates = _format_candidates(
            {"formats": formats},
            max_file_bytes=50,
            quality_policy=VideoQualityPolicy.BEST,
        )
        self.assertEqual([candidate.selector for candidate in candidates], ["v1440"])

    def test_balanced_hls_uses_generic_fallback_when_target_is_missing(self) -> None:
        info = {
            "duration": 100,
            "formats": [
                {
                    "format_id": "audio",
                    "protocol": "m3u8_native",
                    "url": "https://example.com/a.m3u8",
                    "resolution": "audio only",
                    "vcodec": "none",
                    "acodec": "aac",
                },
                {
                    "format_id": "v1080",
                    "protocol": "m3u8_native",
                    "url": "https://example.com/v.m3u8",
                    "height": 1080,
                    "fps": 30,
                    "tbr": 500,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
            ],
        }
        self.assertIsNone(
            _youtube_hls_clip_streams(
                info,
                30,
                45 * 1024 * 1024,
                VideoQualityPolicy.BALANCED,
            )
        )

    def test_transfer_projection_requires_bytes_and_uses_total_time(self) -> None:
        self.assertIsNone(_projected_transfer_seconds(5, 0, 100))
        self.assertEqual(_projected_transfer_seconds(5, 10, 100), 50)

    def test_excludes_source_candidates_above_bounded_size(self) -> None:
        candidate = {
            "format_id": "v4k",
            "height": 2160,
            "filesize": 1000,
            "vcodec": "h264",
            "acodec": "aac",
        }
        self.assertEqual(_format_candidates({"formats": [candidate]}, max_file_bytes=100), [])

    def test_clip_estimates_hls_size_and_prefers_segmented_source(self) -> None:
        source_duration = 1880
        formats = [
            {
                "format_id": "hls1440",
                "protocol": "m3u8_native",
                "height": 1440,
                "fps": 60,
                "tbr": 14085,
                "vcodec": "vp9",
                "acodec": "none",
            },
            {
                "format_id": "http1440",
                "protocol": "https",
                "height": 1440,
                "fps": 60,
                "filesize": 1_356_000_000,
                "vcodec": "av1",
                "acodec": "none",
            },
            {
                "format_id": "hls720",
                "protocol": "m3u8_native",
                "height": 720,
                "fps": 60,
                "tbr": 3806,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "audio",
                "protocol": "https",
                "filesize": 30_400_000,
                "abr": 128,
                "vcodec": "none",
                "acodec": "aac",
            },
        ]
        candidates = _format_candidates(
            {"duration": source_duration, "formats": formats},
            max_file_bytes=45 * 1024 * 1024,
            duration_scale=60 / source_duration,
        )

        self.assertNotIn("hls1440+audio", [candidate.selector for candidate in candidates])
        self.assertEqual(candidates[0].selector, "hls720+audio")
        self.assertIsNotNone(candidates[0].estimated_size)


@patch(
    "telegram_share_bot.downloader._probe_video_file",
    new=lambda *_: _VideoProbe(60, "h264", "yuv420p", "aac", 1280, 720),
)
class TestYoutubeHlsClip(unittest.TestCase):
    def test_video_clip_downloads_streams_separately_then_merges(self) -> None:
        info = {
            "id": "example1234",
            "title": "Example clip",
            "duration": 1880,
            "ext": "mp4",
            "formats": [
                {
                    "format_id": "234",
                    "protocol": "m3u8_native",
                    "url": "https://manifest.googlevideo.com/audio.m3u8",
                    "resolution": "audio only",
                    "vcodec": "none",
                    "acodec": None,
                },
                {
                    "format_id": "311",
                    "protocol": "m3u8_native",
                    "url": "https://manifest.googlevideo.com/video.m3u8",
                    "height": 720,
                    "fps": 60,
                    "tbr": 3806,
                    "vcodec": "avc1.4d4020",
                    "acodec": "none",
                },
            ],
        }
        commands: list[list[str]] = []

        def fake_ffmpeg(argv: list[str], output: Path, **kwargs: object) -> None:
            _ = kwargs
            commands.append(argv)
            output.write_bytes(b"clip data")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.downloader.shutil.which", return_value="ffmpeg"),
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch(
                    "telegram_share_bot.downloader._safe_dns_resolution",
                    return_value=contextlib.nullcontext(),
                ),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(info, False),
                ),
                patch(
                    "telegram_share_bot.downloader._run_bounded_clip_ffmpeg",
                    side_effect=fake_ffmpeg,
                ),
            ):
                with self.assertLogs("telegram_share_bot.downloader", level="INFO") as logs:
                    media = _download_sync(
                        "https://youtu.be/example1234",
                        Path(tmp),
                        max_file_bytes=45 * 1024 * 1024,
                        timeout_seconds=90,
                        time_range=TimeRange(60, 120),
                    )
            self.assertEqual(media.kind, MediaKind.VIDEO)
            self.assertEqual(media.duration, 60)
            self.assertTrue(media.path.exists())
            self.assertTrue(
                any(
                    "Clip HLS video transfer started: format=311 quality=720p 60fps avc1" in line
                    for line in logs.output
                )
            )
            self.assertEqual(len(commands), 3)
            self.assertIn("0:v:0", commands[0])
            self.assertIn("0:a:0", commands[1])
            self.assertIn("1:a:0", commands[2])

    def test_expired_clip_stage_kills_ffmpeg_process(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        with patch("telegram_share_bot.downloader.subprocess.Popen", return_value=process):
            with self.assertRaises(DownloadError):
                _run_bounded_clip_ffmpeg(
                    ["ffmpeg"],
                    Path("unused.mp4"),
                    deadline=0,
                    abort_event=None,
                    source_limit=1024,
                    timeout_seconds=30,
                )
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_slow_hls_clip_stage_stops_after_five_second_estimate(self) -> None:
        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()

        class Process:
            killed = False

            def wait(self, timeout: float | None = None) -> int:
                if self.killed:
                    return -9
                assert timeout is not None
                clock.now += timeout
                raise subprocess.TimeoutExpired("ffmpeg", timeout)

            def poll(self) -> int | None:
                return -9 if self.killed else None

            def kill(self) -> None:
                self.killed = True

        process = Process()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "clip.mp4"
            output.write_bytes(b"x" * 10)
            with (
                patch("telegram_share_bot.downloader.subprocess.Popen", return_value=process),
                patch("telegram_share_bot.downloader.time", clock),
            ):
                with self.assertRaises(DownloadError) as caught:
                    _run_bounded_clip_ffmpeg(
                        ["ffmpeg"],
                        output,
                        deadline=120,
                        abort_event=None,
                        source_limit=100,
                        timeout_seconds=120,
                        expected_bytes=100,
                        speed_limit_seconds=45,
                        format_id="hls720",
                    )
        self.assertEqual(str(caught.exception), strings.DOWNLOAD_SLOW_SOURCE)
        self.assertTrue(process.killed)

    def test_audio_clip_uses_hls_soundtrack_without_video(self) -> None:
        info = {
            "id": "example1234",
            "title": "Example audio",
            "duration": 1880,
            "ext": "mp4",
            "formats": [
                {
                    "format_id": "234",
                    "protocol": "m3u8_native",
                    "url": "https://manifest.googlevideo.com/audio.m3u8",
                    "resolution": "audio only",
                    "vcodec": "none",
                    "acodec": None,
                }
            ],
        }
        commands: list[list[str]] = []

        def fake_ffmpeg(argv: list[str], output: Path, **kwargs: object) -> None:
            _ = kwargs
            commands.append(argv)
            output.write_bytes(b"audio data")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.downloader.shutil.which", return_value="ffmpeg"),
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch(
                    "telegram_share_bot.downloader._safe_dns_resolution",
                    return_value=contextlib.nullcontext(),
                ),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(info, False),
                ),
                patch(
                    "telegram_share_bot.downloader._run_bounded_clip_ffmpeg",
                    side_effect=fake_ffmpeg,
                ),
            ):
                media = _download_sync(
                    "https://youtu.be/example1234",
                    Path(tmp),
                    max_file_bytes=45 * 1024 * 1024,
                    timeout_seconds=90,
                    time_range=TimeRange(60, 120),
                    media_format=MediaFormat.AUDIO,
                )
            self.assertEqual(media.kind, MediaKind.AUDIO)
            self.assertEqual(media.duration, 60)
            self.assertEqual(media.path.suffix, ".m4a")
            self.assertTrue(media.path.exists())
            self.assertEqual(len(commands), 1)
            self.assertIn("0:a:0", commands[0])


class TestAudioFormatSelection(unittest.TestCase):
    def test_ranks_audio_only_streams_and_skips_unbounded_estimates(self) -> None:
        formats = [
            {
                "format_id": "video",
                "height": 1080,
                "filesize": 10,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "audio-low",
                "abr": 64,
                "filesize": 5,
                "vcodec": "none",
                "acodec": "opus",
            },
            {
                "format_id": "audio-high",
                "abr": 128,
                "filesize": 8,
                "vcodec": "none",
                "acodec": "aac",
            },
            {
                "format_id": "audio-too-large",
                "abr": 256,
                "filesize": 250,
                "vcodec": "none",
                "acodec": "aac",
            },
        ]
        candidates = _audio_format_candidates({"formats": formats}, max_file_bytes=100)
        self.assertEqual(
            [candidate.selector for candidate in candidates],
            ["audio-high", "audio-low"],
        )

    def test_audio_download_uses_audio_only_format_and_returns_native_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selectors: list[str] = []
            formats = [
                {
                    "format_id": "video",
                    "height": 1080,
                    "filesize": 10,
                    "vcodec": "h264",
                    "acodec": "none",
                },
                {
                    "format_id": "audio-low",
                    "abr": 64,
                    "filesize": 5,
                    "vcodec": "none",
                    "acodec": "opus",
                },
                {
                    "format_id": "audio-high",
                    "abr": 128,
                    "filesize": 8,
                    "vcodec": "none",
                    "acodec": "aac",
                },
            ]

            class FakeYdl:
                def __init__(self, opts: dict[str, object]) -> None:
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
                    _ = info, download
                    selector = str(self.params["format"])
                    selectors.append(selector)
                    path = Path(_outtmpl_template(self.params)).parent / "result.m4a"
                    path.write_bytes(b"audio-bytes")
                    return {
                        "title": "Audio test",
                        "requested_downloads": [{"filepath": str(path)}],
                    }

                def prepare_filename(self, info: dict[str, object]) -> str:
                    _ = info
                    return str(Path(_outtmpl_template(self.params)).parent / "result.m4a")

            with (
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
                patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", FakeYdl),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(
                        {"title": "Audio test", "duration": 60, "formats": formats},
                        False,
                    ),
                ),
            ):
                media = _download_sync(
                    "https://youtube.com/watch?v=example",
                    root,
                    max_file_bytes=100,
                    timeout_seconds=10,
                    media_format=MediaFormat.AUDIO,
                )
            self.assertEqual(selectors[0], "audio-high")
            self.assertNotIn("video", selectors)
            self.assertEqual(media.kind, MediaKind.AUDIO)
            self.assertEqual(media.path.suffix, ".m4a")

    def test_missing_audio_stream_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for error_type in (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError):

                class FakeYdl:
                    def __init__(self, opts: dict[str, object]) -> None:
                        self.params = dict(opts)

                    def __enter__(self) -> FakeYdl:
                        return self

                    def __exit__(self, *args: object) -> None:
                        return None

                    def build_format_selector(self, selector: str) -> str:
                        return selector

                    def process_ie_result(
                        self,
                        info: dict[str, object],
                        download: bool = True,
                        error_to_raise: type[Exception] = error_type,
                    ) -> dict[str, object]:
                        _ = info, download
                        raise error_to_raise(
                            "[Reddit] Requested format is not available. "
                            "Use --list-formats for a list of available formats"
                        )

                with (
                    patch(
                        "telegram_share_bot.downloader.is_safe_media_url",
                        return_value=True,
                    ),
                    patch(
                        "telegram_share_bot.downloader._safe_dns_resolution",
                        contextlib.nullcontext,
                    ),
                    patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", FakeYdl),
                    patch(
                        "telegram_share_bot.downloader._extract_info_cached",
                        return_value=(
                            {
                                "title": "No audio",
                                "duration": 60,
                                "formats": [
                                    {
                                        "format_id": "video",
                                        "height": 720,
                                        "vcodec": "h264",
                                        "acodec": "none",
                                    }
                                ],
                            },
                            False,
                        ),
                    ),
                ):
                    with self.subTest(error_type=error_type.__name__):
                        with self.assertRaises(DownloadError) as ctx:
                            _download_sync(
                                "https://reddit.com/r/example/video",
                                Path(tmp),
                                max_file_bytes=100,
                                timeout_seconds=10,
                                media_format=MediaFormat.AUDIO,
                            )
                        self.assertEqual(str(ctx.exception), strings.AUDIO_UNAVAILABLE)


class TestYtDlpOutputTemplate(unittest.TestCase):
    def test_attempt_template_keeps_yt_dlp_mapping_and_prepares_a_filename(self) -> None:
        original_template = "downloads/%(title)s.%(ext)s"
        attempt_template = "downloads/attempt/%(title).80B [%(id)s].%(ext)s"
        with yt_dlp.YoutubeDL({"outtmpl": original_template, "quiet": True}) as ydl:
            original_templates = dict(ydl.params["outtmpl"])
            _set_attempt_output_template(ydl, attempt_template)

            self.assertIsInstance(ydl.params["outtmpl"], dict)
            self.assertEqual(ydl.params["outtmpl"]["default"], attempt_template)
            self.assertEqual(ydl.params["outtmpl"]["chapter"], original_templates["chapter"])
            prepared = ydl.prepare_filename(
                {"title": "Adaptive test", "id": "abc123", "ext": "mp4"}
            )
            self.assertIn("attempt", prepared)
            self.assertTrue(prepared.endswith(".mp4"))

    def test_selector_rebuild_switches_from_video_to_audio_only(self) -> None:
        formats = [
            {
                "format_id": "video720",
                "url": "https://cdn.invalid/video.mp4",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
                "height": 720,
            },
            {
                "format_id": "audio128",
                "url": "https://cdn.invalid/audio.m4a",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 128,
            },
        ]
        with yt_dlp.YoutubeDL({"quiet": True, "format": "bv*+ba/b"}) as ydl:
            initial = ydl._select_formats(formats, ydl.format_selector)
            self.assertEqual([fmt["format_id"] for fmt in initial], ["video720+audio128"])

            _set_attempt_format_selector(ydl, "audio128")
            selected = ydl._select_formats(formats, ydl.format_selector)
            self.assertEqual([fmt["format_id"] for fmt in selected], ["audio128"])


@patch(
    "telegram_share_bot.downloader._probe_video_file",
    new=lambda *_: _VideoProbe(60, "h264", "yuv420p", "aac", 1280, 720),
)
class TestMeasuredFallback(unittest.TestCase):
    def test_auto_steps_down_on_slow_transfer_but_forced_modes_hold_quality(self) -> None:
        formats = [
            {
                "format_id": "v1080",
                "height": 1080,
                "fps": 60,
                "filesize": 55,
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "v720",
                "height": 720,
                "fps": 60,
                "filesize": 38,
                "vcodec": "h264",
                "acodec": "none",
            },
            {"format_id": "audio", "abr": 128, "filesize": 2, "vcodec": "none", "acodec": "aac"},
        ]
        selectors: list[str] = []

        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()

        class FakeYdl:
            def __init__(self, opts: dict[str, object]) -> None:
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
                _ = info, download
                selector = str(self.params["format"])
                selectors.append(selector)
                if selector.startswith("v1080"):
                    hooks = self.params["progress_hooks"]
                    assert isinstance(hooks, list)
                    progress = {
                        "status": "downloading",
                        "downloaded_bytes": 10,
                        "total_bytes": 100,
                        "filename": "v1080.part",
                        "info_dict": {"vcodec": "h264"},
                    }
                    clock.now = 4.9
                    hooks[0](progress)
                    clock.now = 5.1
                    hooks[0](progress)
                path = Path(_outtmpl_template(self.params)).parent / "result.mp4"
                path.write_bytes(b"x" * (45 if selector.startswith("v1080") else 40))
                return {"title": "Quality test", "requested_downloads": [{"filepath": str(path)}]}

            def prepare_filename(self, info: dict[str, object]) -> str:
                _ = info
                return str(Path(_outtmpl_template(self.params)).parent / "result.mp4")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
                patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", FakeYdl),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(
                        {"title": "Quality test", "duration": 60, "formats": formats},
                        False,
                    ),
                ),
                patch("telegram_share_bot.downloader.time", clock),
            ):
                for policy, expected in (
                    (VideoQualityPolicy.AUTO, ["v1080+audio", "v720+audio"]),
                    (VideoQualityPolicy.BEST, ["v1080+audio"]),
                    (VideoQualityPolicy.BALANCED, ["v720+audio"]),
                ):
                    with self.subTest(policy=policy):
                        selectors.clear()
                        clock.now = 0.0
                        media = _download_sync(
                            "https://youtube.com/watch?v=quality",
                            Path(tmp),
                            max_file_bytes=50,
                            timeout_seconds=120,
                            quality_policy=policy,
                            max_estimated_download_seconds=45,
                        )
                        self.assertEqual(selectors, expected)
                        expected_bytes = 45 if policy is VideoQualityPolicy.BEST else 40
                        self.assertEqual(media.path.stat().st_size, expected_bytes)

    def test_retries_lower_quality_after_measured_output_is_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selectors: list[str] = []
            formats = [
                {
                    "format_id": "v720",
                    "height": 720,
                    "fps": 30,
                    "filesize": 30,
                    "vcodec": "h264",
                    "acodec": "none",
                },
                {
                    "format_id": "v480",
                    "height": 480,
                    "fps": 30,
                    "filesize": 20,
                    "vcodec": "h264",
                    "acodec": "none",
                },
                {
                    "format_id": "audio",
                    "abr": 128,
                    "filesize": 2,
                    "vcodec": "none",
                    "acodec": "aac",
                },
            ]

            class FakeYdl:
                def __init__(self, opts: dict[str, object]) -> None:
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
                    selector = str(self.params["format"])
                    selectors.append(selector)
                    path = Path(_outtmpl_template(self.params)).parent / "result.mp4"
                    path.write_bytes(b"x" * (60 if selector.startswith("v720") else 40))
                    return {
                        "title": "Adaptive test",
                        "requested_downloads": [{"filepath": str(path)}],
                    }

                def prepare_filename(self, info: dict[str, object]) -> str:
                    return str(Path(_outtmpl_template(self.params)).parent / "result.mp4")

            with (
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
                patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", FakeYdl),
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(
                        {"title": "Adaptive test", "duration": 60, "formats": formats},
                        False,
                    ),
                ),
            ):
                with self.assertLogs("telegram_share_bot.downloader", level="INFO") as logs:
                    media = _download_sync(
                        "https://youtube.com/watch?v=example",
                        root,
                        max_file_bytes=50,
                        timeout_seconds=10,
                    )
            self.assertIsInstance(media, DownloadedMedia)
            self.assertEqual(selectors[:2], ["v720+audio", "v480+audio"])
            self.assertEqual(media.path.stat().st_size, 40)
            self.assertEqual(media.kind, MediaKind.VIDEO)
            self.assertTrue(any("quality=720p 30fps h264" in line for line in logs.output))
            self.assertTrue(any("quality=480p 30fps h264" in line for line in logs.output))


class TestMediaDurationLimit(unittest.TestCase):
    def test_long_full_media_is_rejected_before_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                patch("telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext),
                patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL") as ydl_class,
                patch(
                    "telegram_share_bot.downloader._extract_info_cached",
                    return_value=(
                        {"id": "long", "title": "Long video", "duration": 3600},
                        False,
                    ),
                ),
            ):
                with self.assertRaises(DownloadError) as ctx:
                    _download_sync(
                        "https://youtube.com/watch?v=long",
                        Path(tmp),
                        max_file_bytes=45 * 1024 * 1024,
                        timeout_seconds=10,
                    )
                ydl_class.return_value.__enter__.return_value.process_ie_result.assert_not_called()
            self.assertIn("30 min", str(ctx.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestVideoIntegrity(unittest.TestCase):
    def test_best_never_returns_leftover_audio_when_video_is_skipped(self) -> None:
        for video_size in (None, 200):
            with self.subTest(video_size=video_size), tempfile.TemporaryDirectory() as tmp:
                ydl = MagicMock()
                ydl.params = {}
                ydl.__enter__.return_value = ydl
                ydl.prepare_filename.return_value = str(Path(tmp) / "missing.mp4")

                def leave_audio(
                    *args: object, ydl: MagicMock = ydl, **kwargs: object
                ) -> dict[str, object]:
                    directory = Path(_outtmpl_template(ydl.params)).parent
                    (directory / "leftover.mp4").write_bytes(b"audio")
                    return {"title": "Test"}

                ydl.process_ie_result.side_effect = leave_audio
                info = {
                    "title": "Test",
                    "duration": 10,
                    "formats": [
                        {
                            "format_id": "video",
                            "vcodec": "vp9",
                            "acodec": "none",
                            "height": 2160,
                            "filesize": video_size,
                        },
                        {"format_id": "audio", "vcodec": "none", "acodec": "opus", "filesize": 2},
                    ],
                }
                with (
                    patch("telegram_share_bot.downloader.yt_dlp.YoutubeDL", return_value=ydl),
                    patch("telegram_share_bot.downloader.is_safe_media_url", return_value=True),
                    patch(
                        "telegram_share_bot.downloader._safe_dns_resolution", contextlib.nullcontext
                    ),
                    patch(
                        "telegram_share_bot.downloader._extract_info_cached",
                        return_value=(info, False),
                    ),
                    patch("telegram_share_bot.downloader.shutil.which", return_value="ffprobe"),
                    patch(
                        "telegram_share_bot.downloader.subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, b'{"streams":[{"codec_type":"audio"}]}'
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(DownloadError, "Try Auto or Balanced"):
                        _download_sync(
                            "https://youtube.com/watch?v=example",
                            Path(tmp),
                            50,
                            10,
                            quality_policy=VideoQualityPolicy.BEST,
                        )
                self.assertEqual(ydl.process_ie_result.call_count, 1 if video_size is None else 0)


class TestVideoOptimization(unittest.TestCase):
    def test_small_incompatible_sources_are_normalized_without_needless_video_encoding(
        self,
    ) -> None:
        for codec, expected in (("h264", "copy"), ("vp9", "libx264")):
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "source.mkv"
                source.write_bytes(b"source")
                commands: list[list[str]] = []

                def encode(
                    argv: list[str],
                    output: Path,
                    commands: list[list[str]] = commands,
                    **kwargs: object,
                ) -> None:
                    commands.append(argv)
                    output.write_bytes(b"normalized")

                with (
                    patch("telegram_share_bot.downloader.shutil.which", return_value="ffmpeg"),
                    patch(
                        "telegram_share_bot.downloader._probe_video_file",
                        side_effect=[
                            _VideoProbe(10, codec, "yuv420p", "opus", 1920, 1080),
                            _VideoProbe(10, "h264", "yuv420p", "aac", 1920, 1080),
                        ],
                    ),
                    patch(
                        "telegram_share_bot.downloader._run_bounded_clip_ffmpeg", side_effect=encode
                    ),
                ):
                    result = _optimize_video_file(
                        source,
                        max_file_bytes=1024 * 1024,
                        deadline=9999999999,
                        abort_event=None,
                        on_optimizing=None,
                    )
                self.assertTrue(result.exists())
                self.assertEqual(commands[0][commands[0].index("-c:v") + 1], expected)

    def test_optimizes_at_most_twice_and_calls_status_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"x" * 30)
            calls = 0
            notices: list[bool] = []

            def fake_run(argv: list[str], output: Path, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                self.assertEqual(argv[argv.index("-i") + 1], str(source))
                self.assertNotIn("-crf", argv)
                self.assertNotIn("1280", argv[argv.index("-vf") + 1])
                output.write_bytes(b"x" * (20 if calls == 1 else 8))

            def notice() -> None:
                notices.append(True)

            with self.assertLogs("telegram_share_bot.downloader", level="INFO") as logs:
                with (
                    patch("telegram_share_bot.downloader.shutil.which", return_value="ffmpeg"),
                    patch(
                        "telegram_share_bot.downloader._run_bounded_clip_ffmpeg",
                        side_effect=fake_run,
                    ),
                    patch(
                        "telegram_share_bot.downloader._probe_video_file",
                        return_value=_VideoProbe(1, "h264", "yuv420p", None, 3840, 2160),
                    ),
                ):
                    result = _optimize_video_file(
                        source,
                        max_file_bytes=10,
                        deadline=9999999999,
                        abort_event=None,
                        on_optimizing=notice,
                    )
            self.assertTrue(any("Optimizing video for Telegram" in line for line in logs.output))
            self.assertEqual(result.stat().st_size, 8)
            self.assertLessEqual(calls, 2)
            self.assertEqual(notices, [True])

    def test_rejects_audio_only_file_despite_mp4_extension(self) -> None:
        with (
            patch("telegram_share_bot.downloader.shutil.which", return_value="ffprobe"),
            patch(
                "telegram_share_bot.downloader.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    b'{"streams":[{"codec_type":"audio","codec_name":"opus"}],"format":{"duration":"297"}}',
                ),
            ),
        ):
            with self.assertRaises(DownloadError):
                _probe_video_file(Path("audio.mp4"), 9999999999)

    def test_compatible_small_video_is_not_reencoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"video")
            with (
                patch(
                    "telegram_share_bot.downloader._probe_video_file",
                    return_value=_VideoProbe(10, "h264", "yuv420p", "aac", 1920, 1080),
                ),
                patch("telegram_share_bot.downloader._run_bounded_clip_ffmpeg") as encode,
            ):
                result = _optimize_video_file(
                    source,
                    max_file_bytes=100,
                    deadline=9999999999,
                    abort_event=None,
                    on_optimizing=None,
                )
            self.assertEqual(result, source)
            encode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
