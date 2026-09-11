"""Unit tests for cache validation & graceful fallback behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest

from telegram_share_bot.cache import MediaCache
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import DirectMediaStream, DownloadedMedia, MediaKind
from telegram_share_bot.handlers import _prepare_inline_media, url_message


class TestCacheFallback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_fallback.db"
        self.cache = MediaCache(self.db_path)
        self.settings = Settings(
            bot_token="test:token",
            storage_chat_id=-100123456789,
            max_file_bytes=50 * 1024 * 1024,
            download_timeout_seconds=30,
            download_dir=Path(self.temp_dir.name) / "downloads",
            cache_db_path=self.db_path,
            delete_storage_messages=False,
            allow_public=True,
            max_downloads_per_user=3,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_url_message_cache_hit_and_evict_on_bad_request(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        stale_file_id = "STALE_FILE_ID_123"

        # Pre-seed cache with stale file_id
        await self.cache.set(
            url=url,
            file_id=stale_file_id,
            kind=MediaKind.VIDEO,
            title="Old Title",
            duration=100,
        )

        # Mock Telegram context and update
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }

        # Mock bot sending: first call (cached) raises BadRequest, second call (fresh) succeeds
        context.bot.send_video = AsyncMock()
        context.bot.send_video.side_effect = [
            BadRequest("Wrong file identifier/HTTP URL specified"),
            MagicMock(video=MagicMock(file_id="NEW_FRESH_FILE_ID")),
        ]

        update = MagicMock()
        message = MagicMock()
        message.text = url
        message.chat_id = 123456
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_msg)
        update.effective_message = message

        work_dir = Path(self.temp_dir.name) / "work1"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "video.mp4",
            title="Fresh Download",
            kind=MediaKind.VIDEO,
            duration=120,
        )
        fresh_media.path.write_bytes(b"dummy video data")

        download_mock = AsyncMock(return_value=fresh_media)
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", AsyncMock(return_value=None)),
            patch("telegram_share_bot.handlers.download_media", download_mock),
        ):
            await url_message(update, context)

        # Verify:
        # 1. The bot attempted to send cached media first and threw BadRequest
        self.assertEqual(context.bot.send_video.call_count, 2)
        # 2. Fresh download succeeded and replaced the stale file_id in cache with NEW_FRESH_FILE_ID
        updated = await self.cache.get(url)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.file_id, "NEW_FRESH_FILE_ID")

    async def test_inline_prepare_cache_hit_and_evict_on_bad_request(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        stale_file_id = "STALE_INLINE_FILE_ID"

        await self.cache.set(
            url=url,
            file_id=stale_file_id,
            kind=MediaKind.VIDEO,
            title="Stale Inline",
            duration=60,
        )

        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
            "inline_prepare_tasks": {},
            "cancelled_inline": set(),
        }

        # First edit_message_media fails with BadRequest (stale file_id),
        # second call succeeds after fresh upload
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_media.side_effect = [
            BadRequest("Wrong remote file identifier specified"),
            None,
        ]
        context.bot.edit_message_text = AsyncMock()

        work_dir = Path(self.temp_dir.name) / "work2"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "inline_video.mp4",
            title="Fresh Inline",
            kind=MediaKind.VIDEO,
            duration=60,
        )
        fresh_media.path.write_bytes(b"dummy inline data")

        download_mock = AsyncMock(return_value=fresh_media)
        upload_mock = AsyncMock(
            return_value=("FRESH_INLINE_FILE_ID", "Fresh Inline", MediaKind.VIDEO)
        )
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", AsyncMock(return_value=None)),
            patch("telegram_share_bot.handlers.download_media", download_mock),
            patch("telegram_share_bot.handlers._upload_for_file_id", upload_mock),
        ):
            await _prepare_inline_media(
                context,
                inline_message_id="msg_xyz",
                url=url,
                result_id="res_123",
            )

        # edit_message_media was called twice (once failed, once succeeded with fresh download)
        self.assertEqual(context.bot.edit_message_media.call_count, 2)
        # Cache was updated with new file_id
        updated = await self.cache.get(url)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.file_id, "FRESH_INLINE_FILE_ID")

    async def test_inline_prepare_uses_direct_stream(self) -> None:
        url = "https://x.com/example/status/12345"
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        mock_stream = DirectMediaStream(
            direct_url="https://video.twimg.com/test.mp4",
            title="Twitter Video",
            kind=MediaKind.VIDEO,
            duration=30,
        )

        stream_mock = AsyncMock(return_value=mock_stream)
        direct_upload = AsyncMock(
            return_value=("DIRECT_FILE_ID_789", "Twitter Video", MediaKind.VIDEO)
        )
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", stream_mock),
            patch(
                "telegram_share_bot.handlers._upload_direct_url_for_file_id",
                direct_upload,
            ) as mock_direct_upload,
            patch("telegram_share_bot.handlers.download_media") as mock_download,
        ):
            await _prepare_inline_media(
                context,
                inline_message_id="msg_direct",
                url=url,
                result_id="res_direct",
            )

            # Direct upload was called and download_media was skipped
            mock_direct_upload.assert_awaited_once()
            mock_download.assert_not_called()
            context.bot.edit_message_media.assert_awaited_once()

            cached = await self.cache.get(url)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.file_id, "DIRECT_FILE_ID_789")

    async def test_direct_stream_fallback_to_download_when_telegram_fails(self) -> None:
        url = "https://x.com/example/status/67890"
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        mock_stream = DirectMediaStream(
            direct_url="https://video.twimg.com/failed.mp4",
            title="Fallback Video",
            kind=MediaKind.VIDEO,
            duration=15,
        )

        work_dir = Path(self.temp_dir.name) / "work3"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "fallback_video.mp4",
            title="Fallback Video",
            kind=MediaKind.VIDEO,
            duration=15,
        )
        fresh_media.path.write_bytes(b"dummy")

        stream_mock = AsyncMock(return_value=mock_stream)
        download_mock = AsyncMock(return_value=fresh_media)
        upload_mock = AsyncMock(
            return_value=("LOCAL_FALLBACK_FILE_ID", "Fallback Video", MediaKind.VIDEO)
        )
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", stream_mock),
            patch(
                "telegram_share_bot.handlers._upload_direct_url_for_file_id",
                AsyncMock(return_value=None),  # Direct upload failed
            ),
            patch("telegram_share_bot.handlers.download_media", download_mock),
            patch("telegram_share_bot.handlers._upload_for_file_id", upload_mock),
        ):
            await _prepare_inline_media(
                context,
                inline_message_id="msg_fallback",
                url=url,
                result_id="res_fallback",
            )

            context.bot.edit_message_media.assert_awaited_once()
            cached = await self.cache.get(url)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.file_id, "LOCAL_FALLBACK_FILE_ID")


if __name__ == "__main__":
    unittest.main()
