"""Unit tests for download timeout handling and guaranteed cleanup."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_share_bot.downloader import (
    DownloadError,
    _download_sync,
    download_media,
)


class TestDownloadTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_download_timeout_defers_cleanup_until_worker_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            download_dir = Path(tmp_dir)
            worker_started = threading.Event()
            allow_worker_to_finish = threading.Event()
            worker_finished = threading.Event()
            partial_file = download_dir / "slow_test_uuid" / "partial.mp4.part"

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
                _ = url, max_file_bytes, timeout_seconds, abort_event, https_only
                created = download_dir / "slow_test_uuid"
                created.mkdir(parents=True, exist_ok=True)
                if work_dir_holder is not None:
                    work_dir_holder.append(created)
                try:
                    with partial_file.open("wb") as partial:
                        partial.write(b"partial video data")
                        partial.flush()
                        worker_started.set()
                        allow_worker_to_finish.wait(timeout=2)
                        partial.write(b"finished")
                    raise DownloadError("worker finished after outer timeout")
                finally:
                    worker_finished.set()

            with patch(
                "telegram_share_bot.downloader._download_sync",
                side_effect=slow_download,
            ):
                request = asyncio.create_task(
                    download_media(
                        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        download_dir=download_dir,
                        max_file_bytes=10 * 1024 * 1024,
                        timeout_seconds=0.1,
                    )
                )
                try:
                    self.assertTrue(await asyncio.to_thread(worker_started.wait, 2))
                    with self.assertRaises(DownloadError) as ctx:
                        await request
                    self.assertIn("Download timed out", str(ctx.exception))
                    self.assertFalse(worker_finished.is_set())
                    self.assertTrue(partial_file.exists())
                finally:
                    allow_worker_to_finish.set()
                    self.assertTrue(await asyncio.to_thread(worker_finished.wait, 2))

            for _ in range(100):
                if not list(download_dir.iterdir()):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(list(download_dir.iterdir()), [])

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
