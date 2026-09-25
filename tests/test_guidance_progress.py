"""Tests for onboarding guidance and visible media preparation stages."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.cache import MediaCache
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import DownloadedMedia, MediaKind, TimeRange
from telegram_share_bot.handlers import (
    _prepare_inline_media,
    _run_direct_download,
    help_command,
    inline_query,
    start_command,
)


class TestBotGuidance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = MagicMock()
        self.context.bot.username = "share_bot"
        self.context.bot.first_name = "Share Bot"
        self.context.application.bot_data = {
            "settings": Settings(
                bot_token="test:token",
                storage_chat_id=42,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=Path("."),
                cache_db_path=Path("cache.db"),
                delete_storage_messages=False,
                allow_public=True,
            ),
        }
        self.message = MagicMock()
        self.message.reply_text = AsyncMock()
        self.update = MagicMock()
        self.update.effective_message = self.message
        self.update.effective_chat = MagicMock()
        self.update.effective_user = MagicMock(id=42)

    async def test_start_is_short_and_points_to_inline_sharing(self) -> None:
        await start_command(self.update, self.context)

        args = self.message.reply_text.await_args
        self.assertIn("🎬 Share Bot", args.args[0])
        self.assertIn("@share_bot", args.args[0])
        self.assertIn("YouTube · TikTok · Instagram · X", args.args[0])
        self.assertIn("TikTok photo slideshows", args.args[0])
        self.assertIn("YouTube clips or full videos", args.args[0])
        self.assertIn("/help", args.args[0])
        self.assertNotIn("1:20-2:05", args.args[0])
        self.assertNotIn("reply_markup", args.kwargs)

    async def test_help_has_scannable_instructions_and_actual_caption_mode(self) -> None:
        await help_command(self.update, self.context)

        args = self.message.reply_text.await_args
        text = args.args[0]
        self.assertIn("📖 How to share", text)
        self.assertIn("@share_bot", text)
        self.assertIn("1:20-2:05", text)
        self.assertIn("full video", text)
        self.assertIn(strings.HELP_CAPTION_MEDIA, text)
        self.assertIn("multi-item collections are not supported", text)
        self.assertNotIn("reply_markup", args.kwargs)


class TestMediaProgress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "progress.db"
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
        self.context = MagicMock()
        self.context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
            "inline_prepare_tasks": {},
            "cancelled_inline": set(),
        }

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _media(self, folder: str) -> DownloadedMedia:
        work_dir = Path(self.temp_dir.name) / folder
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / "video.mp4"
        path.write_bytes(b"media")
        return DownloadedMedia(path, "Example video", MediaKind.VIDEO, 30)

    async def test_direct_chat_shows_download_upload_and_done_states(self) -> None:
        status = MagicMock()
        status.edit_text = AsyncMock()
        media = self._media("direct")
        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(return_value=media),
            ),
            patch(
                "telegram_share_bot.handlers._send_media_to_chat",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "telegram_share_bot.handlers._file_id_and_kind_from_message",
                return_value=("FILE_ID", MediaKind.VIDEO),
            ),
        ):
            await _run_direct_download(
                self.context,
                chat_id=42,
                user_id=42,
                url="https://example.com/video",
                custom_caption=None,
                time_range=None,
                status_message=status,
            )

        self.assertEqual(
            [call.args[0] for call in status.edit_text.await_args_list],
            [
                strings.DIRECT_DOWNLOADING,
                strings.DIRECT_UPLOADING,
                strings.DIRECT_DONE,
            ],
        )

    async def test_inline_flow_shows_check_download_and_upload_states(self) -> None:
        self.context.bot.edit_message_text = AsyncMock()
        self.context.bot.edit_message_media = AsyncMock()
        media = self._media("inline")
        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(return_value=media),
            ),
            patch(
                "telegram_share_bot.handlers._upload_for_file_id",
                new=AsyncMock(
                    return_value=("FILE_ID", "Example video", MediaKind.VIDEO)
                ),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-progress",
                url="https://example.com/video",
                result_id="progress-result",
                user_id=42,
            )

        states = [
            call.kwargs["text"]
            for call in self.context.bot.edit_message_text.await_args_list
        ]
        self.assertEqual(
            states,
            [
                strings.INLINE_DOWNLOADING.format(url="https://example.com/video"),
                strings.INLINE_UPLOADING.format(url="https://example.com/video"),
            ],
        )

    async def test_inline_choices_use_known_details_without_remote_metadata(self) -> None:
        url = "https://youtu.be/GKq9nKZpmu0"
        clip = TimeRange(150, 210)
        await self.cache.set(
            url, "CLIP_ID", MediaKind.VIDEO, "Example", 60, time_range=clip
        )
        await self.cache.set(url, "FULL_ID", MediaKind.VIDEO, "Example", 360)
        query = MagicMock()
        query.from_user = MagicMock(id=42)
        query.query = f"{url} 2:30-3:30"
        query.answer = AsyncMock()
        update = MagicMock()
        update.inline_query = query

        with (
            patch("telegram_share_bot.handlers.get_direct_stream") as direct,
            patch("telegram_share_bot.handlers.download_media") as download,
        ):
            await inline_query(update, self.context)
        direct.assert_not_called()
        download.assert_not_called()
        clip_choice, full_choice = query.answer.await_args.kwargs["results"]
        self.assertIn("Send clip 2:30-3:30", clip_choice.title)
        self.assertIn(
            "YouTube · Video · Duration: 1:00 · Instant",
            clip_choice.description,
        )
        self.assertIn("Checking clip", clip_choice.input_message_content.message_text)
        self.assertEqual(
            clip_choice.thumbnail_url,
            "https://i.ytimg.com/vi/GKq9nKZpmu0/mqdefault.jpg",
        )
        self.assertEqual(clip_choice.thumbnail_width, 320)
        self.assertEqual(clip_choice.thumbnail_height, 180)
        self.assertIn("Send full video", full_choice.title)
        self.assertIn("YouTube · Video · Duration: 6:00 · Instant", full_choice.description)
        self.assertEqual(full_choice.thumbnail_url, clip_choice.thumbnail_url)

    async def test_uncached_inline_video_has_action_and_readiness(self) -> None:
        query = MagicMock()
        query.from_user = MagicMock(id=42)
        query.query = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        query.answer = AsyncMock()
        update = MagicMock()
        update.inline_query = query

        await inline_query(update, self.context)

        result = query.answer.await_args.kwargs["results"][0]
        self.assertEqual(result.title, "▶ Send video")
        self.assertEqual(
            result.description, "YouTube · Video · Download"
        )
        self.assertIn("Checking the media link", result.input_message_content.message_text)
        self.assertEqual(
            result.thumbnail_url,
            "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg",
        )

    async def test_inline_non_youtube_link_falls_back_to_platform_logo(self) -> None:
        query = MagicMock()
        query.from_user = MagicMock(id=42)
        query.query = "https://www.tiktok.com/@user/video/123456"
        query.answer = AsyncMock()
        update = MagicMock()
        update.inline_query = query

        await inline_query(update, self.context)

        result = query.answer.await_args.kwargs["results"][0]
        self.assertTrue(result.thumbnail_url.endswith("/tiktok.png"))
        self.assertEqual((result.thumbnail_width, result.thumbnail_height), (224, 224))

    async def test_clip_progress_starts_after_initial_checking_message(self) -> None:
        self.context.bot.edit_message_text = AsyncMock()
        self.context.bot.edit_message_media = AsyncMock()
        media = self._media("clip")
        with (
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(return_value=media),
            ),
            patch(
                "telegram_share_bot.handlers._upload_for_file_id",
                new=AsyncMock(return_value=("FILE_ID", "Clip", MediaKind.VIDEO)),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="clip-progress",
                url="https://youtu.be/GKq9nKZpmu0",
                result_id="clip-result",
                user_id=42,
                time_range=TimeRange(150, 210),
            )

        states = [
            call.kwargs["text"]
            for call in self.context.bot.edit_message_text.await_args_list
        ]
        display_url = "https://www.youtube.com/watch?v=GKq9nKZpmu0"
        self.assertEqual(
            states,
            [
                strings.INLINE_DOWNLOADING.format(url=display_url),
                strings.INLINE_UPLOADING.format(url=display_url),
            ],
        )


if __name__ == "__main__":
    unittest.main()
