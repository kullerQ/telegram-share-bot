"""Unit tests for download timeout handling and guaranteed cleanup."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_share_bot.downloader import (
    DownloadError,
    _download_sync,
    download_media,
)


class TestDownloadTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_download_media_cleans_up_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            download_dir = Path(tmp_dir)

            # A slow download function that sleeps longer than timeout
            def slow_download(
                url,
                download_dir,
                max_file_bytes,
                timeout_seconds,
                abort_event=None,
                work_dir_holder=None,
                https_only=False,
                **_kwargs,
            ):
                created = download_dir / "slow_test_uuid"
                created.mkdir(parents=True, exist_ok=True)
                (created / "partial.mp4").write_bytes(b"partial video data")
                if work_dir_holder is not None:
                    work_dir_holder.append(created)

                # Wait for timeout to trigger
                time.sleep(0.3)
                return MagicMock()

            with patch("telegram_share_bot.downloader._download_sync", side_effect=slow_download):
                with self.assertRaises(DownloadError) as ctx:
                    await download_media(
                        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        download_dir=download_dir,
                        max_file_bytes=10 * 1024 * 1024,
                        timeout_seconds=0.1,  # Short timeout
                    )
                self.assertIn("Download timed out", str(ctx.exception))

            # Ensure the directory was cleaned up on timeout
            remaining_dirs = list(download_dir.glob("*"))
            self.assertEqual(len(remaining_dirs), 0)

    def test_progress_hook_aborts_when_cancelled(self) -> None:
        abort_event = threading.Event()
        abort_event.set()

        with tempfile.TemporaryDirectory() as tmp_dir:
            download_dir = Path(tmp_dir)
            with self.assertRaises(DownloadError) as ctx:
                _download_sync(
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    download_dir=download_dir,
                    max_file_bytes=10 * 1024 * 1024,
                    timeout_seconds=5,
                    abort_event=abort_event,
                )
            self.assertIn("timed out", str(ctx.exception).lower())

            # Verify no orphaned folders remain
            self.assertEqual(len(list(download_dir.glob("*"))), 0)

    def test_live_stream_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            download_dir = Path(tmp_dir)
            mock_ydl = MagicMock()
            mock_ydl.extract_info.return_value = {
                "id": "live123",
                "title": "Live Stream",
                "is_live": True,
            }

            with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
                mock_ydl_cls.return_value.__enter__.return_value = mock_ydl
                with self.assertRaises(DownloadError) as ctx:
                    _download_sync(
                        url="https://www.youtube.com/watch?v=live123",
                        download_dir=download_dir,
                        max_file_bytes=10 * 1024 * 1024,
                        timeout_seconds=5,
                    )
                self.assertIn("Live streams cannot be downloaded", str(ctx.exception))
                self.assertEqual(len(list(download_dir.glob("*"))), 0)


if __name__ == "__main__":
    unittest.main()
