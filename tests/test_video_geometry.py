"""Display geometry parsing and propagation for verified video files."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot.media.models import PreparedVideo
from telegram_share_bot.media.transcode import _optimize_video_file, _probe_video_file


def _ffmpeg_input_log(video_streams: str) -> bytes:
    return (
        b"  Duration: 00:00:01.00, start: 0.000000, bitrate: 100 kb/s\n"
        + video_streams.encode()
        + b"Stream mapping:\n  Stream #0:0 -> #0:0 (h264 -> wrapped_avframe)\n"
    )


class TestVideoDisplayGeometry(unittest.TestCase):
    def _probe_log(self, video_streams: str):
        with (
            patch("telegram_share_bot.media.transcode.shutil.which", return_value="ffmpeg"),
            patch(
                "telegram_share_bot.media.transcode.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, b"", _ffmpeg_input_log(video_streams)
                ),
            ),
        ):
            return _probe_video_file(Path("geometry.mp4"), time.monotonic() + 30)

    def test_square_landscape_and_portrait_use_display_dimensions(self) -> None:
        examples = (
            ("Video: h264, yuv420p, 600x600\n", (600, 600)),
            ("Video: h264, yuv420p, 1920x1080\n", (1920, 1080)),
            ("Video: h264, yuv420p, 720x1280\n", (720, 1280)),
        )
        for stream, expected in examples:
            with self.subTest(expected=expected):
                probe = self._probe_log(f"  Stream #0:0: {stream}")
                self.assertEqual(probe.display_dimensions, expected)

    def test_sample_aspect_ratio_adjusts_display_width(self) -> None:
        probe = self._probe_log(
            "  Stream #0:0: Video: h264, yuv420p, 720x576 [SAR 16:15 DAR 4:3]\n"
        )
        self.assertEqual(probe.display_dimensions, (768, 576))

    def test_rotation_and_metadata_from_only_first_actual_video_stream(self) -> None:
        probe = self._probe_log(
            "  Stream #0:0: Video: mjpeg, yuvj420p, 600x600 (attached pic)\n"
            "  Stream #0:1: Video: h264, yuv420p, 720x576 [SAR 16:15 DAR 4:3]\n"
            "    Side data:\n"
            "      displaymatrix: rotation of -90.00 degrees\n"
            "  Stream #0:2: Video: vp9, yuv420p, 1920x1080 [SAR 2:1 DAR 32:9]\n"
            "    Side data:\n"
            "      displaymatrix: rotation of 180.00 degrees\n"
        )
        self.assertEqual(probe.width, 720)
        self.assertEqual(probe.height, 576)
        self.assertEqual(probe.sample_aspect_ratio, (16, 15))
        self.assertEqual(probe.rotation_degrees, -90)
        self.assertEqual(probe.display_dimensions, (576, 768))

    def test_invalid_sar_and_later_stream_side_data_are_ignored(self) -> None:
        probe = self._probe_log(
            "  Stream #0:0: Video: h264, yuv420p, 720x576 [SAR 0:1 DAR 4:3]\n"
            "  Stream #0:1: Video: vp9, yuv420p, 1920x1080 [SAR 16:15 DAR 16:9]\n"
            "    Side data:\n"
            "      displaymatrix: rotation of 90.00 degrees\n"
        )
        self.assertIsNone(probe.sample_aspect_ratio)
        self.assertEqual(probe.rotation_degrees, 0.0)
        self.assertEqual(probe.display_dimensions, (720, 576))

    def test_old_probe_construction_defaults_to_square_pixels_and_no_rotation(self) -> None:
        from telegram_share_bot.media.transcode import _VideoProbe

        probe = _VideoProbe(1, "h264", "yuv420p", None, 720, 1280)
        self.assertEqual(probe.display_dimensions, (720, 1280))


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for media integration checks")
class TestPreparedVideoGeometryWithFFmpeg(unittest.TestCase):
    def _run_ffmpeg(self, *args: str) -> None:
        result = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            self.fail(
                f"FFmpeg command failed ({result.returncode}): "
                f"{result.stderr.decode('utf-8', errors='replace')}"
            )

    def test_compatible_source_is_not_encoded_again_and_geometry_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:r=5:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(source),
            )
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch("telegram_share_bot.media.transcode._run_bounded_clip_ffmpeg") as encode:
                prepared = _optimize_video_file(
                    source,
                    max_file_bytes=2 * 1024 * 1024,
                    deadline=time.monotonic() + 30,
                    abort_event=None,
                    on_optimizing=None,
                )
            self.assertEqual(prepared, PreparedVideo(source, 320, 240))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            encode.assert_not_called()

    def test_real_anamorphic_probe_reports_display_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "anamorphic.mkv"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=720x576:r=2:d=1",
                "-vf",
                "setsar=16/15",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            )
            probe = _probe_video_file(source, time.monotonic() + 30)
            self.assertEqual(probe.sample_aspect_ratio, (16, 15))
            self.assertEqual(probe.display_dimensions, (768, 576))

    def test_real_rotated_probe_swaps_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            rotated = Path(tmp) / "rotated.mp4"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:r=2:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            )
            self._run_ffmpeg(
                "-display_rotation:v:0",
                "90",
                "-i",
                str(source),
                "-c",
                "copy",
                str(rotated),
            )
            probe = _probe_video_file(rotated, time.monotonic() + 30)
            self.assertEqual(probe.width, 320)
            self.assertEqual(probe.height, 240)
            self.assertIn(abs(probe.rotation_degrees), (90, 270))
            self.assertEqual(probe.display_dimensions, (240, 320))

    def test_converted_output_reports_final_display_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=320x240:r=5:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv444p",
                str(source),
            )
            prepared = _optimize_video_file(
                source,
                max_file_bytes=2 * 1024 * 1024,
                deadline=time.monotonic() + 30,
                abort_event=None,
                on_optimizing=None,
            )
            self.assertNotEqual(prepared.path, source)
            self.assertTrue(prepared.path.is_file())
            self.assertEqual((prepared.width, prepared.height), (320, 240))

    def test_converted_anamorphic_output_preserves_display_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "anamorphic.mkv"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=720x576:r=2:d=1",
                "-vf",
                "setsar=16/15,format=yuv444p",
                "-c:v",
                "libx264",
                str(source),
            )
            prepared = _optimize_video_file(
                source,
                max_file_bytes=2 * 1024 * 1024,
                deadline=time.monotonic() + 30,
                abort_event=None,
                on_optimizing=None,
            )
            self.assertEqual((prepared.width, prepared.height), (768, 576))


if __name__ == "__main__":
    unittest.main()
