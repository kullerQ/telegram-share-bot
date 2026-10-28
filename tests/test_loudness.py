"""Focused tests for automatic quiet-audio normalization."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_share_bot.media import loudness
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaKind


def _measurement(input_i: str = "-30.0", input_tp: str = "-8.0") -> str:
    return "input loudnorm report\n" + json.dumps(
        {
            "input_i": input_i,
            "input_tp": input_tp,
            "input_lra": "5.0",
            "input_thresh": "-40.0",
        }
    )


class TestLoudnessTarget(unittest.TestCase):
    def test_target_caps_the_integrated_loudness_increase_at_24_lu(self) -> None:
        self.assertEqual(loudness._target_lufs(-30.0), -16.0)
        self.assertAlmostEqual(loudness._target_lufs(-43.5), -19.5)
        self.assertEqual(loudness._target_lufs(-50.0), -26.0)

    def test_quiet_boundary_does_not_call_targeting_for_loud_media(self) -> None:
        media = DownloadedMedia(Path("unused"), "title", MediaKind.AUDIO, 12)
        with (
            patch.object(loudness.shutil, "which", return_value="ffmpeg"),
            patch.object(
                loudness, "_run_ffmpeg_bounded", return_value=_measurement("-20.0", "-8.0")
            ) as run,
        ):
            self.assertIs(
                loudness.normalize_quiet_audio(
                    media,
                    max_file_bytes=10_000,
                    deadline=time.monotonic() + 30,
                    abort_event=None,
                ),
                media,
            )
        run.assert_called_once()

    def test_non_finite_measurements_are_rejected(self) -> None:
        for value in (float("-inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loudness._target_lufs(value)
        self.assertIsNone(loudness._parse_measurement(_measurement("-inf", "-3.0")))


class TestNormalizeQuietAudio(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tempdir.cleanup)
        self.source = Path(self.tempdir.name) / "source.bin"
        self.source.write_bytes(b"source")

    def _media(self, kind: MediaKind = MediaKind.AUDIO) -> DownloadedMedia:
        return DownloadedMedia(self.source, "title", kind, 12, 640, 360)

    def test_skips_animations_and_documents_without_running_ffmpeg(self) -> None:
        for kind in (MediaKind.ANIMATION, MediaKind.DOCUMENT):
            media = self._media(kind)
            with self.subTest(kind=kind), patch.object(loudness.shutil, "which") as which:
                self.assertIs(
                    loudness.normalize_quiet_audio(
                        media,
                        max_file_bytes=10_000,
                        deadline=time.monotonic() + 30,
                        abort_event=None,
                    ),
                    media,
                )
                which.assert_not_called()

    def test_skips_missing_audio_and_silence_measurements(self) -> None:
        for report in (
            "Output file #0 does not contain any stream",
            _measurement("-inf", "-inf"),
        ):
            with (
                self.subTest(report=report),
                patch.object(loudness.shutil, "which", return_value="ffmpeg"),
                patch.object(loudness, "_run_ffmpeg_bounded", return_value=report),
            ):
                media = self._media()
                self.assertIs(
                    loudness.normalize_quiet_audio(
                        media,
                        max_file_bytes=10_000,
                        deadline=time.monotonic() + 30,
                        abort_event=None,
                    ),
                    media,
                )

    def test_loud_audio_is_left_untouched(self) -> None:
        media = self._media()
        with (
            patch.object(loudness.shutil, "which", return_value="ffmpeg"),
            patch.object(
                loudness, "_run_ffmpeg_bounded", return_value=_measurement("-19.9", "-8")
            ) as run,
        ):
            result = loudness.normalize_quiet_audio(
                media,
                max_file_bytes=10_000,
                deadline=time.monotonic() + 30,
                abort_event=None,
            )
        self.assertIs(result, media)
        run.assert_called_once()

    def test_video_uses_loudnorm_and_copies_video_bitstream(self) -> None:
        media = self._media(MediaKind.VIDEO)
        command: list[str] = []

        def run(argv: list[str], **kwargs: object) -> str:
            nonlocal command
            command = argv
            output = Path(argv[-1])
            output.write_bytes(b"normalized")
            return ""

        calls = 0

        def run_measurement_then_apply(argv: list[str], **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return _measurement() if calls == 1 else run(argv, **kwargs)

        with (
            patch.object(loudness.shutil, "which", return_value="ffmpeg"),
            patch.object(loudness, "_run_ffmpeg_bounded", side_effect=run_measurement_then_apply),
        ):
            result = loudness.normalize_quiet_audio(
                media,
                max_file_bytes=10_000,
                deadline=time.monotonic() + 30,
                abort_event=None,
            )
        self.assertEqual(result.path.suffix, ".mp4")
        self.assertEqual((result.width, result.height), (640, 360))
        self.assertIn("copy", command)
        self.assertIn(
            "loudnorm=I=-16.00:TP=-2.5:LRA=11.00:measured_I=-30.00:"
            "measured_TP=-8.00:measured_LRA=5.00:measured_thresh=-40.00:linear=true",
            command,
        )
        self.assertIn("48000", command)
        self.assertIn("+faststart", command)
        self.assertFalse(self.source.exists())

    def test_second_pass_failure_removes_partial_output(self) -> None:
        def fail_apply(argv: list[str], **kwargs: object) -> str:
            output = Path(argv[-1])
            output.write_bytes(b"partial")
            raise DownloadError("retry", retryable=True)

        calls = 0

        def run_measurement_then_fail(argv: list[str], **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return _measurement() if calls == 1 else fail_apply(argv, **kwargs)

        media = self._media()
        with (
            patch.object(loudness.shutil, "which", return_value="ffmpeg"),
            patch.object(loudness, "_run_ffmpeg_bounded", side_effect=run_measurement_then_fail),
        ):
            with self.assertRaises(DownloadError):
                loudness.normalize_quiet_audio(
                    media,
                    max_file_bytes=10_000,
                    deadline=time.monotonic() + 30,
                    abort_event=None,
                )
        self.assertEqual(list(Path(self.tempdir.name).glob("*_normalized.*")), [])
        self.assertTrue(self.source.exists())

    def test_expired_deadline_fails_before_starting_ffmpeg(self) -> None:
        with (
            patch.object(loudness.shutil, "which", return_value="ffmpeg"),
            patch.object(loudness.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(DownloadError) as raised:
                loudness.normalize_quiet_audio(
                    self._media(),
                    max_file_bytes=10_000,
                    deadline=time.monotonic() - 1,
                    abort_event=threading.Event(),
                )
        self.assertIn("timed out", str(raised.exception).lower())
        popen.assert_not_called()


class TestLoudnessProcessLifetime(unittest.TestCase):
    def test_cancellation_stops_and_reaps_the_actual_process(self) -> None:
        event = threading.Event()
        process = MagicMock()
        calls = 0

        def wait(*args: object, **kwargs: object) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                event.set()
                raise subprocess.TimeoutExpired("ffmpeg", 0.2)
            return 0

        process.wait.side_effect = wait
        process.poll.return_value = None
        with patch.object(loudness.subprocess, "Popen", return_value=process):
            with self.assertRaises(DownloadError):
                loudness._run_ffmpeg_bounded(
                    ["ffmpeg"],
                    output=None,
                    max_file_bytes=1000,
                    deadline=time.monotonic() + 10,
                    abort_event=event,
                )
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_nonzero_measurement_exit_rejects_even_valid_looking_json(self) -> None:
        def start(argv: list[str], **kwargs: object) -> MagicMock:
            kwargs["stderr"].write(_measurement().encode())
            process = MagicMock()
            process.wait.return_value = 1
            process.poll.return_value = 1
            return process

        with patch.object(loudness.subprocess, "Popen", side_effect=start):
            with self.assertRaises(DownloadError):
                loudness._run_ffmpeg_bounded(
                    ["ffmpeg"],
                    output=None,
                    max_file_bytes=1000,
                    deadline=time.monotonic() + 10,
                    abort_event=None,
                )

    def test_output_size_limit_stops_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "large.mp4"
            output.write_bytes(b"too large")
            process = MagicMock()
            process.poll.return_value = None
            with patch.object(loudness.subprocess, "Popen", return_value=process):
                with self.assertRaises(DownloadError):
                    loudness._run_ffmpeg_bounded(
                        ["ffmpeg"],
                        output=output,
                        max_file_bytes=1,
                        deadline=time.monotonic() + 10,
                        abort_event=None,
                    )
            process.kill.assert_called_once()
            process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
