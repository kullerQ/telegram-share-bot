"""Tests for onboarding guidance and visible media preparation stages."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.cache import MediaCache
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import (
    DirectMediaStream,
    DownloadedMedia,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
)
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

    async def test_direct_chat_shows_optimization_and_clears_status_after_send(self) -> None:
        status = MagicMock()
        status.edit_text = AsyncMock()
        status.delete = AsyncMock()
        media = self._media("direct")

        async def download_with_optimization(**kwargs: object) -> DownloadedMedia:
            callback = kwargs["on_optimizing"]
            assert callable(callback)
            await asyncio.to_thread(callback)
            return media

        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(side_effect=download_with_optimization),
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
                strings.OPTIMIZING_FOR_TELEGRAM,
                strings.DIRECT_UPLOADING,
            ],
        )
        status.delete.assert_awaited_once()

    async def test_direct_audio_without_a_stream_shows_specific_feedback(self) -> None:
        status = MagicMock()
        status.edit_text = AsyncMock()
        status.delete = AsyncMock()
        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(side_effect=DownloadError(strings.AUDIO_UNAVAILABLE)),
            ),
        ):
            await _run_direct_download(
                self.context,
                chat_id=42,
                user_id=42,
                url="https://reddit.com/r/example/video",
                custom_caption=None,
                time_range=None,
                status_message=status,
                media_format=MediaFormat.AUDIO,
            )

        self.assertEqual(
            status.edit_text.await_args_list[-1].args[0],
            strings.AUDIO_UNAVAILABLE,
        )
        status.delete.assert_not_awaited()

    async def test_long_direct_stream_is_rejected_before_upload(self) -> None:
        stream = DirectMediaStream(
            direct_url="https://example.com/long.mp4",
            title="Long video",
            kind=MediaKind.VIDEO,
            duration=3600,
            size_bytes=1024,
        )
        status = MagicMock()
        status.edit_text = AsyncMock()
        upload = AsyncMock()
        self.context.bot.edit_message_text = AsyncMock()
        expected = strings.DOWNLOAD_MEDIA_TOO_LONG.format(duration_limit="30 min")

        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=stream),
            ),
            patch(
                "telegram_share_bot.handlers._upload_direct_url_for_file_id",
                new=upload,
            ),
        ):
            await _run_direct_download(
                self.context,
                chat_id=42,
                user_id=42,
                url="https://example.com/long",
                custom_caption=None,
                time_range=None,
                status_message=status,
            )
            await _prepare_inline_media(
                self.context,
                inline_message_id="long-inline",
                url="https://example.com/long",
                result_id="long-result",
                user_id=42,
            )

        self.assertEqual(status.edit_text.await_args_list[-1].args[0], expected)
        self.assertEqual(
            self.context.bot.edit_message_text.await_args_list[-1].kwargs["text"],
            expected,
        )
        upload.assert_not_awaited()

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
        clip_choice, audio_clip, full_choice, full_audio = query.answer.await_args.kwargs["results"]
        self.assertIn("Send video clip 2:30-3:30", clip_choice.title)
        self.assertIn("YouTube · Video · 2:30-3:30 · Cached", clip_choice.description)
        self.assertIn("Send audio clip 2:30-3:30", audio_clip.title)
        self.assertIn("YouTube · Audio · 2:30-3:30 · Download", audio_clip.description)
        self.assertIn("Checking clip", clip_choice.input_message_content.message_text)
        self.assertEqual(
            clip_choice.thumbnail_url,
            "https://i.ytimg.com/vi/GKq9nKZpmu0/mqdefault.jpg",
        )
        self.assertEqual(clip_choice.thumbnail_width, 320)
        self.assertEqual(clip_choice.thumbnail_height, 180)
        self.assertIn("Send full video", full_choice.title)
        self.assertIn("YouTube · Video · 6:00 · Cached", full_choice.description)
        self.assertIn("Send full audio", full_audio.title)
        self.assertEqual(full_choice.thumbnail_url, clip_choice.thumbnail_url)

    async def test_uncached_inline_video_has_action_and_readiness(self) -> None:
        query = MagicMock()
        query.from_user = MagicMock(id=42)
        query.query = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        query.answer = AsyncMock()
        update = MagicMock()
        update.inline_query = query

        await inline_query(update, self.context)

        video, audio = query.answer.await_args.kwargs["results"]
        self.assertEqual(video.title, "▶ Send video")
        self.assertEqual(video.description, "YouTube · Video · Download")
        self.assertEqual(audio.title, "♫ Send audio")
        self.assertEqual(audio.description, "YouTube · Audio · Download")
        result = video
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

        with patch(
            "telegram_share_bot.handlers.platform_previews.resolve_preview",
            new=AsyncMock(return_value=None),
        ):
            await inline_query(update, self.context)

        result = query.answer.await_args.kwargs["results"][0]
        self.assertTrue(result.thumbnail_url.endswith("/tiktok.png"))
        self.assertEqual((result.thumbnail_width, result.thumbnail_height), (224, 224))

    async def test_reddit_and_facebook_inline_links_keep_logo_on_preview_failure(self) -> None:
        for url, filename in (
            ("https://www.reddit.com/r/videos/comments/abc123/example/", "reddit.png"),
            ("https://www.facebook.com/reel/123456789", "facebook.png"),
        ):
            with self.subTest(url=url):
                query = MagicMock()
                query.from_user = MagicMock(id=42)
                query.query = url
                query.answer = AsyncMock()
                update = MagicMock()
                update.inline_query = query
                with patch(
                    "telegram_share_bot.handlers.platform_previews.resolve_preview",
                    new=AsyncMock(return_value=None),
                ):
                    await inline_query(update, self.context)
                result = query.answer.await_args.kwargs["results"][0]
                self.assertTrue(result.thumbnail_url.endswith("/" + filename))
                platform = "Reddit" if filename == "reddit.png" else "Facebook"
                self.assertIn(platform, result.description)

    async def test_inline_audio_choice_uses_audio_direct_stream_and_cache_variant(self) -> None:
        self.context.bot.edit_message_text = AsyncMock()
        self.context.bot.edit_message_media = AsyncMock()
        audio_path = Path(self.temp_dir.name) / "audio-choice" / "audio.m4a"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        audio = DownloadedMedia(audio_path, "Example audio", MediaKind.AUDIO, 30)
        direct = AsyncMock(return_value=None)
        download = AsyncMock(return_value=audio)
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", direct),
            patch("telegram_share_bot.handlers.download_media", download),
            patch(
                "telegram_share_bot.handlers._upload_for_file_id",
                AsyncMock(return_value=("AUDIO_FILE_ID", "Example audio", MediaKind.AUDIO)),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-audio-choice",
                url="https://youtube.com/watch?v=example",
                result_id="audio-choice",
                user_id=42,
                media_format=MediaFormat.AUDIO,
            )

        self.assertIs(direct.await_args.kwargs["media_format"], MediaFormat.AUDIO)
        self.assertIs(download.await_args.kwargs["media_format"], MediaFormat.AUDIO)
        cached = await self.cache.get(
            "https://youtube.com/watch?v=example", media_format=MediaFormat.AUDIO
        )
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "AUDIO_FILE_ID")
        self.assertIsNone(
            await self.cache.get(
                "https://youtube.com/watch?v=example", media_format=MediaFormat.VIDEO
            )
        )

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
