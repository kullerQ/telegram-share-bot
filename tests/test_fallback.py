"""Unit tests for cache validation & graceful fallback behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType
from telegram.error import BadRequest, NetworkError

from telegram_share_bot import strings
from telegram_share_bot.cache import MediaCache
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import (
    DirectMediaStream,
    DownloadedMedia,
    MediaFormat,
    MediaKind,
    VideoQualityPolicy,
)
from telegram_share_bot.handlers import (
    PendingClipChoice,
    _prepare_inline_media,
    _run_direct_download,
    direct_format_callback,
    url_message,
    video_command,
)


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

    async def test_auto_send_uses_cached_best_video_without_downloading(self) -> None:
        url = "https://youtu.be/quality1234"
        await self.cache.set(
            url, "AUTO_ID", MediaKind.VIDEO, "Auto", 40,
            quality_policy=VideoQualityPolicy.AUTO.value, video_height=720,
        )
        await self.cache.set(
            url, "BEST_ID", MediaKind.VIDEO, "Best", 40,
            quality_policy=VideoQualityPolicy.BEST.value, video_height=1080,
        )
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.send_video = AsyncMock()
        status = MagicMock()
        status.edit_text = AsyncMock()
        status.delete = AsyncMock()
        with patch("telegram_share_bot.handlers.download_media", AsyncMock()) as download:
            await _run_direct_download(
                context, chat_id=42, user_id=42, url=url,
                custom_caption=None, time_range=None, status_message=status,
            )
        self.assertEqual(context.bot.send_video.await_args.kwargs["video"], "BEST_ID")
        download.assert_not_awaited()

    async def test_private_url_offers_video_audio_and_selected_cache_fallback(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        stale_file_id = "STALE_FILE_ID_123"
        await self.cache.set(
            url=url,
            file_id=stale_file_id,
            kind=MediaKind.VIDEO,
            title="Old Title",
            duration=100,
            quality_policy=VideoQualityPolicy.AUTO.value,
        )

        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.send_video = AsyncMock()
        context.bot.send_video.side_effect = [
            BadRequest("Wrong file identifier/HTTP URL specified"),
            MagicMock(video=MagicMock(file_id="NEW_FRESH_FILE_ID")),
        ]

        update = MagicMock()
        message = MagicMock()
        message.text = url
        message.chat_id = 123456
        status_msg = MagicMock(spec=__import__("telegram").Message)
        status_msg.edit_text = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_msg)
        update.effective_message = message
        update.effective_user = MagicMock(id=42)

        await url_message(update, context)
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.callback_data.split(":", 1)[0] for button in keyboard.inline_keyboard[0]],
            ["video", "audio"],
        )
        self.assertEqual(
            [button.callback_data.split(":", 1)[0] for button in keyboard.inline_keyboard[1]],
            ["video-best", "video-balanced"],
        )
        self.assertEqual(context.bot.send_video.call_count, 0)

        choice_id = keyboard.inline_keyboard[0][0].callback_data.split(":", 1)[1]
        context.application.bot_data["pending_clip_choice"] = {
            choice_id: PendingClipChoice(url, None, None, message.chat_id)
        }
        update.callback_query = MagicMock()
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.data = f"video:{choice_id}"
        update.callback_query.message = status_msg
        update.callback_query.answer = AsyncMock()

        work_dir = Path(self.temp_dir.name) / "work1"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "video.mp4",
            title="Fresh Download",
            kind=MediaKind.VIDEO,
            duration=120,
        )
        fresh_media.path.write_bytes(b"dummy video data")
        with (
            patch("telegram_share_bot.handlers.get_direct_stream", AsyncMock(return_value=None)),
            patch(
                "telegram_share_bot.handlers.download_media",
                AsyncMock(return_value=fresh_media),
            ),
        ):
            await direct_format_callback(update, context)

        self.assertEqual(context.bot.send_video.call_count, 2)
        updated = await self.cache.get(
            url,
            media_format=MediaFormat.VIDEO,
            quality_policy=VideoQualityPolicy.AUTO.value,
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.file_id, "NEW_FRESH_FILE_ID")

    async def test_private_quality_buttons_pass_the_selected_policy(self) -> None:
        context = MagicMock()
        context.application.bot_data = {"settings": self.settings}
        status = MagicMock(spec=__import__("telegram").Message)
        status.edit_text = AsyncMock()

        for prefix, expected in (
            ("video-best", VideoQualityPolicy.BEST),
            ("video-balanced", VideoQualityPolicy.BALANCED),
        ):
            with self.subTest(prefix=prefix):
                choice_id = prefix
                context.application.bot_data["pending_clip_choice"] = {
                    choice_id: PendingClipChoice(
                        "https://youtu.be/example1234", None, None, 42
                    )
                }
                update = MagicMock()
                update.callback_query.from_user = MagicMock(id=42)
                update.callback_query.data = f"{prefix}:{choice_id}"
                update.callback_query.message = status
                update.callback_query.answer = AsyncMock()
                with patch(
                    "telegram_share_bot.handlers._run_direct_download", new=AsyncMock()
                ) as run_download:
                    await direct_format_callback(update, context)
                self.assertEqual(run_download.await_args.kwargs["quality_policy"], expected)

    async def test_video_command_accepts_best_and_balanced_before_link(self) -> None:
        context = MagicMock()
        context.application.bot_data = {"settings": self.settings}
        update = MagicMock()
        update.effective_chat.type = ChatType.PRIVATE
        update.effective_user.id = 42
        status = MagicMock(spec=__import__("telegram").Message)
        update.effective_message.reply_text = AsyncMock(return_value=status)

        for mode, expected in (
            ("best", VideoQualityPolicy.BEST),
            ("balanced", VideoQualityPolicy.BALANCED),
        ):
            with self.subTest(mode=mode):
                update.effective_message.text = f"/video {mode} https://youtu.be/example1234"
                with patch(
                    "telegram_share_bot.handlers._run_direct_download", new=AsyncMock()
                ) as run_download:
                    await video_command(update, context)
                self.assertEqual(run_download.await_args.kwargs["quality_policy"], expected)

    async def test_inline_prepare_cache_hit_and_evict_on_bad_request(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        stale_file_id = "STALE_INLINE_FILE_ID"

        await self.cache.set(
            url=url,
            file_id=stale_file_id,
            kind=MediaKind.VIDEO,
            title="Stale Inline",
            duration=60,
            quality_policy=VideoQualityPolicy.AUTO.value,
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
            return_value=("FRESH_INLINE_FILE_ID", "Fresh Inline", MediaKind.VIDEO, 1080)
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
        updated = await self.cache.get(url, quality_policy=VideoQualityPolicy.AUTO.value)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.file_id, "FRESH_INLINE_FILE_ID")

    async def test_inline_audio_prepare_uses_direct_stream(self) -> None:
        url = "https://x.com/example/status/12345"
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        mock_stream = DirectMediaStream(
            direct_url="https://video.twimg.com/test.m4a",
            title="Twitter Audio",
            kind=MediaKind.AUDIO,
            duration=30,
        )

        stream_mock = AsyncMock(return_value=mock_stream)
        direct_upload = AsyncMock(
            return_value=("DIRECT_FILE_ID_789", "Twitter Audio", MediaKind.AUDIO)
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
                media_format=MediaFormat.AUDIO,
            )

            # Direct upload was called and download_media was skipped
            mock_direct_upload.assert_awaited_once()
            mock_download.assert_not_called()
            context.bot.edit_message_media.assert_awaited_once()

            cached = await self.cache.get(url, media_format=MediaFormat.AUDIO)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.file_id, "DIRECT_FILE_ID_789")

    async def test_direct_audio_stream_fallback_to_download_when_telegram_fails(self) -> None:
        url = "https://x.com/example/status/67890"
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
        }
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        mock_stream = DirectMediaStream(
            direct_url="https://video.twimg.com/failed.m4a",
            title="Fallback Audio",
            kind=MediaKind.AUDIO,
            duration=15,
        )

        work_dir = Path(self.temp_dir.name) / "work3"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "fallback_audio.m4a",
            title="Fallback Audio",
            kind=MediaKind.AUDIO,
            duration=15,
        )
        fresh_media.path.write_bytes(b"dummy")

        stream_mock = AsyncMock(return_value=mock_stream)
        download_mock = AsyncMock(return_value=fresh_media)
        upload_mock = AsyncMock(
            return_value=("LOCAL_FALLBACK_FILE_ID", "Fallback Audio", MediaKind.AUDIO, None)
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
                media_format=MediaFormat.AUDIO,
            )

            context.bot.edit_message_media.assert_awaited_once()
            cached = await self.cache.get(url, media_format=MediaFormat.AUDIO)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.file_id, "LOCAL_FALLBACK_FILE_ID")

    async def test_inline_prepare_handles_network_error_on_upload(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        context = MagicMock()
        context.application.bot_data = {
            "settings": self.settings,
            "media_cache": self.cache,
            "inline_prepare_tasks": {},
            "cancelled_inline": set(),
        }
        context.bot.edit_message_media = AsyncMock()
        context.bot.edit_message_text = AsyncMock()

        work_dir = Path(self.temp_dir.name) / "work_net"
        work_dir.mkdir(parents=True, exist_ok=True)
        fresh_media = DownloadedMedia(
            path=work_dir / "video.mp4",
            title="Net Fail",
            kind=MediaKind.VIDEO,
            duration=10,
        )
        fresh_media.path.write_bytes(b"dummy")

        with (
            patch("telegram_share_bot.handlers.get_direct_stream", AsyncMock(return_value=None)),
            patch(
                "telegram_share_bot.handlers.download_media",
                AsyncMock(return_value=fresh_media),
            ),
            patch(
                "telegram_share_bot.handlers._upload_for_file_id",
                AsyncMock(side_effect=NetworkError("httpx.ReadError: ")),
            ),
        ):
            await _prepare_inline_media(
                context,
                inline_message_id="msg_net",
                url=url,
                result_id="res_net",
            )

        context.bot.edit_message_media.assert_not_awaited()
        edit_calls = context.bot.edit_message_text.await_args_list
        self.assertTrue(edit_calls)
        last_text = edit_calls[-1].kwargs.get("text") or ""
        self.assertEqual(last_text, strings.INLINE_CHOSEN_UPLOAD_FAILED)

    async def test_send_media_retries_transient_network_error(self) -> None:
        from telegram_share_bot.handlers import _send_media_to_chat

        work_dir = Path(self.temp_dir.name) / "work_retry"
        work_dir.mkdir(parents=True, exist_ok=True)
        media = DownloadedMedia(
            path=work_dir / "video.mp4",
            title="Retry",
            kind=MediaKind.VIDEO,
            duration=5,
        )
        media.path.write_bytes(b"video-bytes")

        context = MagicMock()
        context.application.bot_data = {"settings": self.settings}
        ok_msg = MagicMock(video=MagicMock(file_id="OK_AFTER_RETRY"))
        context.bot.send_video = AsyncMock(
            side_effect=[NetworkError("httpx.ReadError: "), ok_msg]
        )

        with patch("telegram_share_bot.handlers.asyncio.sleep", AsyncMock()):
            result = await _send_media_to_chat(
                context,
                chat_id=self.settings.storage_chat_id,
                media=media,
                settings=self.settings,
                force_storage_caption=True,
            )

        self.assertEqual(result, ok_msg)
        self.assertEqual(context.bot.send_video.await_count, 2)


if __name__ == "__main__":
    unittest.main()
