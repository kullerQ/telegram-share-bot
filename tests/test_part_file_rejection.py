"""Unit tests verifying that incomplete .part / .ytdl files are rejected."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from telegram_share_bot import strings
from telegram_share_bot.downloader import (
    DownloadError,
    _resolve_downloaded_path,
)


class TestPartFileRejection(unittest.TestCase):
    def test_only_part_file_raises_incomplete_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            part_file = work_dir / "large_video.mp4.part"
            part_file.write_bytes(b"partial video data")

            mock_ydl = MagicMock()
            mock_ydl.prepare_filename.return_value = str(work_dir / "large_video.mp4")

            with self.assertRaises(DownloadError) as ctx:
                _resolve_downloaded_path({}, work_dir, mock_ydl)

            self.assertEqual(str(ctx.exception), strings.DOWNLOAD_FAILED_INCOMPLETE)

    def test_only_ytdl_temp_file_raises_incomplete_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            (work_dir / "video.mkv.ytdl").write_bytes(b"partial data")

            mock_ydl = MagicMock()
            mock_ydl.prepare_filename.return_value = str(work_dir / "video.mkv")

            with self.assertRaises(DownloadError) as ctx:
                _resolve_downloaded_path({}, work_dir, mock_ydl)

            self.assertEqual(str(ctx.exception), strings.DOWNLOAD_FAILED_INCOMPLETE)

    def test_completed_file_selected_over_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            complete_file = work_dir / "final_video.mp4"
            complete_file.write_bytes(b"complete valid video")
            part_file = work_dir / "audio.m4a.part"
            part_file.write_bytes(b"leftover partial")

            mock_ydl = MagicMock()
            mock_ydl.prepare_filename.return_value = str(complete_file)

            resolved = _resolve_downloaded_path({}, work_dir, mock_ydl)
            self.assertEqual(resolved, complete_file)

    def test_final_video_selected_over_downloaded_audio_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            final_video = work_dir / "merged.mp4"
            final_video.write_bytes(b"complete video")
            audio_component = work_dir / "audio.m4a"
            audio_component.write_bytes(b"audio component")

            mock_ydl = MagicMock()
            mock_ydl.prepare_filename.return_value = str(final_video)
            info = {
                "filepath": str(final_video),
                "requested_downloads": [{"filepath": str(audio_component)}],
            }

            self.assertEqual(_resolve_downloaded_path(info, work_dir, mock_ydl), final_video)

    def test_empty_work_dir_raises_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            mock_ydl = MagicMock()
            mock_ydl.prepare_filename.return_value = str(work_dir / "nonexistent.mp4")

            with self.assertRaises(DownloadError) as ctx:
                _resolve_downloaded_path({}, work_dir, mock_ydl)

            self.assertEqual(str(ctx.exception), strings.DOWNLOAD_NO_FILE)


if __name__ == "__main__":
    unittest.main()
