"""Unit tests for concurrency limiting and download semaphore."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_MAX_DOWNLOADS_PER_USER,
    DEFAULT_MAX_MEDIA_DURATION_SECONDS,
    Settings,
    load_settings,
)
from telegram_share_bot.downloader import DownloadedMedia, MediaKind
from telegram_share_bot.handlers import (
    _release_user_download_slot,
    _try_acquire_user_download_slot,
    direct_format_callback,
    url_message,
)


class TestConcurrencyLimiter(unittest.IsolatedAsyncioTestCase):
    def test_settings_max_concurrent_downloads_parsing(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "MAX_CONCURRENT_DOWNLOADS": "5",
            "ALLOW_PUBLIC": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.max_concurrent_downloads, 5)

    def test_settings_max_concurrent_downloads_default(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOW_PUBLIC": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings()
            self.assertEqual(
                settings.max_concurrent_downloads, DEFAULT_MAX_CONCURRENT_DOWNLOADS
            )
            self.assertEqual(settings.max_downloads_per_user, DEFAULT_MAX_DOWNLOADS_PER_USER)
            self.assertEqual(
                settings.max_media_duration_seconds, DEFAULT_MAX_MEDIA_DURATION_SECONDS
            )

    def test_settings_zero_disables_limits(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOW_PUBLIC": "true",
            "MAX_CONCURRENT_DOWNLOADS": "0",
            "MAX_DOWNLOADS_PER_USER": "0",
            "MAX_MEDIA_DURATION_SECONDS": "0",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings()
            self.assertEqual(settings.max_concurrent_downloads, 0)
            self.assertEqual(settings.max_downloads_per_user, 0)
            self.assertEqual(settings.max_media_duration_seconds, 0)

    async def test_one_user_cannot_occupy_multiple_download_slots(self) -> None:
        settings = Settings(
            bot_token="test:token",
            storage_chat_id=42,
            max_file_bytes=1024,
            download_timeout_seconds=10,
            download_dir=Path("."),
            cache_db_path=Path("cache.db"),
            delete_storage_messages=False,
            download_cooldown_seconds=0,
        )
        context = MagicMock()
        context.application.bot_data = {"settings": settings}

        self.assertIsNone(await _try_acquire_user_download_slot(context, 42))
        self.assertEqual(
            await _try_acquire_user_download_slot(context, 42),
            strings.RATE_LIMITED,
        )
        self.assertIsNone(await _try_acquire_user_download_slot(context, 43))
        await _release_user_download_slot(context, 42)
        self.assertIsNone(await _try_acquire_user_download_slot(context, 42))

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
        mock_settings.max_downloads_per_user = 5
        mock_settings.download_cooldown_seconds = 0
        # Explicit public mode for the concurrency stress test.
        mock_settings.allowed_user_ids = frozenset()
        mock_settings.allow_public = True
        mock_settings.allowed_media_hosts = None
        mock_settings.https_only = True

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
            callbacks = []
            for _ in range(5):
                update = MagicMock()
                update.effective_user = MagicMock(id=42)
                message = MagicMock()
                message.text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                message.chat_id = 12345
                status_message = MagicMock(spec=__import__("telegram").Message)
                status_message.edit_text = AsyncMock()
                message.reply_text = AsyncMock(return_value=status_message)
                update.effective_message = message
                await url_message(update, context)

                keyboard = message.reply_text.await_args.kwargs["reply_markup"]
                choice_id = keyboard.inline_keyboard[0][0].callback_data.split(":", 1)[1]
                update.callback_query = MagicMock()
                update.callback_query.from_user = MagicMock(id=42)
                update.callback_query.data = f"video:{choice_id}"
                update.callback_query.message = status_message
                update.callback_query.answer = AsyncMock()
                callbacks.append(direct_format_callback(update, context))

            await asyncio.gather(*callbacks)

        # Verify that concurrency never exceeded the semaphore limit
        self.assertLessEqual(max_seen_active, semaphore_limit)
        self.assertGreaterEqual(max_seen_active, 1)


if __name__ == "__main__":
    unittest.main()
