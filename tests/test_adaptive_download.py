"""Adaptive Telegram-size video selection and optimization tests."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot.downloader import (
    DownloadedMedia,
    MediaKind,
    _download_sync,
    _format_candidates,
    _optimize_video_file,
)


class TestFormatRanking(unittest.TestCase):
    def test_prefers_best_quality_that_plausibly_fits(self) -> None:
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
            ["v720+audio", "v480+audio", "v1080+audio"],
        )

    def test_excludes_source_candidates_above_bounded_size(self) -> None:
        candidate = {
            "format_id": "v4k",
            "height": 2160,
            "filesize": 1000,
            "vcodec": "h264",
            "acodec": "aac",
        }
        self.assertEqual(_format_candidates({"formats": [candidate]}, max_file_bytes=100), [])


class TestMeasuredFallback(unittest.TestCase):
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

                def process_ie_result(
                    self, info: dict[str, object], download: bool = True
                ) -> dict[str, object]:
                    selector = str(self.params["format"])
                    selectors.append(selector)
                    path = Path(str(self.params["outtmpl"])).parent / "result.mp4"
                    path.write_bytes(b"x" * (60 if selector.startswith("v720") else 40))
                    return {
                        "title": "Adaptive test",
                        "requested_downloads": [{"filepath": str(path)}],
                    }

                def prepare_filename(self, info: dict[str, object]) -> str:
                    return str(Path(str(self.params["outtmpl"])).parent / "result.mp4")

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


class TestVideoOptimization(unittest.TestCase):
    def test_optimizes_at_most_twice_and_calls_status_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"x" * 30)
            calls = 0
            notices: list[bool] = []

            def fake_run(argv: list[str], **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                output = Path(argv[-1])
                output.write_bytes(b"x" * (20 if calls == 1 else 8))
                return type("Completed", (), {"returncode": 0})()

            def notice() -> None:
                notices.append(True)

            with (
                patch("telegram_share_bot.downloader.shutil.which", return_value="ffmpeg"),
                patch("telegram_share_bot.downloader.subprocess.run", side_effect=fake_run),
            ):
                result = _optimize_video_file(
                    source,
                    max_file_bytes=10,
                    deadline=9999999999,
                    abort_event=None,
                    on_optimizing=notice,
                )
            self.assertEqual(result.stat().st_size, 8)
            self.assertLessEqual(calls, 2)
            self.assertEqual(notices, [True])


if __name__ == "__main__":
    unittest.main()
