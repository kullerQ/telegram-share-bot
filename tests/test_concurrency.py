"""Unit tests for concurrency limiting and download semaphore."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot.config import DEFAULT_MAX_CONCURRENT_DOWNLOADS, load_settings
from telegram_share_bot.downloader import DownloadedMedia, MediaKind
from telegram_share_bot.handlers import url_message


class TestConcurrencyLimiter(unittest.IsolatedAsyncioTestCase):
    def test_settings_max_concurrent_downloads_parsing(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "MAX_CONCURRENT_DOWNLOADS": "5",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.max_concurrent_downloads, 5)

    def test_settings_max_concurrent_downloads_default(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings()
            self.assertEqual(
                settings.max_concurrent_downloads, DEFAULT_MAX_CONCURRENT_DOWNLOADS
            )

    async def test_semaphore_limits_parallel_downloads(self) -> None:
        semaphore_limit = 2
        sem = asyncio.Semaphore(semaphore_limit)

        from telegram_share_bot.cache import MediaCache
        from telegram_share_bot.config import Settings

        mock_cache = MagicMock(spec=MediaCache)
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        mock_settings = MagicMock(spec=Settings)
        mock_settings.max_file_bytes = 10 * 1024 * 1024
        mock_settings.download_timeout_seconds = 10
        mock_settings.download_dir = MagicMock()
        mock_settings.upload_timeout_seconds = 10

        context = MagicMock()
        context.application.bot_data = {
            "download_semaphore": sem,
            "media_cache": mock_cache,
            "settings": mock_settings,
        }

        active_downloads = 0
        max_seen_active = 0

        async def fake_download(*args, **kwargs):
            nonlocal active_downloads, max_seen_active
            active_downloads += 1
            max_seen_active = max(max_seen_active, active_downloads)
            await asyncio.sleep(0.05)
            active_downloads -= 1
            dummy = MagicMock(spec=DownloadedMedia)
            dummy.path = MagicMock()
            dummy.title = "Test"
            dummy.kind = MediaKind.VIDEO
            dummy.duration = 10
            return dummy

        update = MagicMock()
        msg = MagicMock()
        msg.text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        msg.chat_id = 12345
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=status_msg)
        update.effective_message = msg

        with (
            patch("telegram_share_bot.handlers.get_direct_stream", AsyncMock(return_value=None)),
            patch("telegram_share_bot.handlers.download_media", side_effect=fake_download),
            patch("telegram_share_bot.handlers._send_media_to_chat", AsyncMock()),
            patch(
                "telegram_share_bot.handlers._file_id_and_kind_from_message",
                return_value=("file_123", MediaKind.VIDEO),
            ),
            patch("telegram_share_bot.handlers.cleanup_media"),
        ):
            # Run 5 concurrent url_message requests
            tasks = [url_message(update, context) for _ in range(5)]
            await asyncio.gather(*tasks)

        # Verify that concurrency never exceeded the semaphore limit
        self.assertLessEqual(max_seen_active, semaphore_limit)
        self.assertGreaterEqual(max_seen_active, 1)


if __name__ == "__main__":
    unittest.main()
