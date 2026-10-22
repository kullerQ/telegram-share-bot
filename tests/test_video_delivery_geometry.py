"""Display geometry is explicit, validated, and retained across Telegram sends."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.config import Settings
from telegram_share_bot.handlers.delivery import (
    _input_media,
    _send_cached_media_to_chat,
    _send_media_to_chat,
    _upload_for_file_id,
)
from telegram_share_bot.handlers.direct import _run_direct_download
from telegram_share_bot.handlers.preferences import _RenderedCaption
from telegram_share_bot.handlers.prepare import _prepare_inline_media
from telegram_share_bot.handlers.trace import _SendTrace
from telegram_share_bot.media.models import (
    DownloadedMedia,
    MediaFormat,
    MediaKind,
    VideoMetadataError,
)
from telegram_share_bot.storage.media_cache import CachedMedia, MediaCache
from telegram_share_bot.storage.user_settings import UserSharingSettings


def _message(kind: MediaKind, width: object = 720, height: object = 1280, file_id: str = "good"):
    visual = SimpleNamespace(width=width, height=height, file_id=file_id)
    return SimpleNamespace(
        video=visual if kind is MediaKind.VIDEO else None,
        animation=visual if kind is MediaKind.ANIMATION else None,
        audio=None,
        document=None,
        message_id=123,
    )


class TestVideoDeliveryGeometry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            bot_token="test:token",
            storage_chat_id=1,
            max_file_bytes=1024,
            download_timeout_seconds=10,
            download_dir=self.root / "downloads",
            cache_db_path=self.root / "cache.db",
            delete_storage_messages=True,
            allow_public=True,
            download_cooldown_seconds=0,
        )
        self.cache = MediaCache(self.settings.cache_db_path)
        self.context = MagicMock()
        self.context.application.bot_data = {"settings": self.settings, "media_cache": self.cache}
        self.context.bot.delete_message = AsyncMock()
        self.context.bot.edit_message_media = AsyncMock()
        self.context.bot.edit_message_text = AsyncMock()
        self.context.bot.send_video = AsyncMock(return_value=_message(MediaKind.VIDEO))
        self.context.bot.send_animation = AsyncMock(return_value=_message(MediaKind.ANIMATION))
        work = self.root / "media"
        work.mkdir()
        path = work / "video.mp4"
        path.write_bytes(b"original video bytes")
        self.media = DownloadedMedia(path, "Title", MediaKind.VIDEO, 12, 720, 1280)
        self.url = "https://www.tiktok.com/@example/video/123"
        self.preferences = UserSharingSettings()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_fresh_and_cached_video_and_animation_keep_portrait_geometry(self) -> None:
        for kind in (MediaKind.VIDEO, MediaKind.ANIMATION):
            with self.subTest(kind=kind):
                sender = (
                    self.context.bot.send_video
                    if kind is MediaKind.VIDEO
                    else self.context.bot.send_animation
                )
                media = DownloadedMedia(self.media.path, "Title", kind, 12, 720, 1280)
                await _send_media_to_chat(self.context, 1, media, self.settings)
                self.assertEqual(sender.await_args.kwargs["width"], 720)
                self.assertEqual(sender.await_args.kwargs["height"], 1280)
                cached = CachedMedia(self.url, "good", kind, "Title", 12, 1280, 720)
                await _send_cached_media_to_chat(self.context, 1, cached)
                self.assertEqual(sender.await_args.kwargs["width"], 720)
                self.assertEqual(sender.await_args.kwargs["height"], 1280)
                inline = _input_media(
                    "good", "Title", kind, caption=_RenderedCaption(None), width=720, height=1280
                )
                self.assertEqual((inline.width, inline.height), (720, 1280))
        self.assertEqual(self.media.path.read_bytes(), b"original video bytes")

    async def test_invalid_server_geometry_is_not_retried_or_kept(self) -> None:
        for width, height in ((0, 0), (720, 720), (None, 1280), (True, 1280)):
            with self.subTest(width=width, height=height):
                self.context.bot.send_video.reset_mock()
                self.context.bot.delete_message.reset_mock()
                self.context.bot.send_video.return_value = _message(MediaKind.VIDEO, width, height)
                with self.assertRaises(VideoMetadataError) as caught:
                    await _send_media_to_chat(self.context, 1, self.media, self.settings)
                self.assertTrue(caught.exception.retryable)
                self.context.bot.send_video.assert_awaited_once()
                self.context.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=123)

    async def test_missing_local_dimensions_fail_before_upload(self) -> None:
        media = DownloadedMedia(self.media.path, "Title", MediaKind.VIDEO, 12)
        with self.assertRaises(VideoMetadataError):
            await _send_media_to_chat(self.context, 1, media, self.settings)
        self.context.bot.send_video.assert_not_awaited()

    async def test_scaled_server_dimensions_preserve_shape_and_storage_geometry(self) -> None:
        self.context.bot.send_video.return_value = _message(MediaKind.VIDEO, 360, 640)
        result = await _upload_for_file_id(self.context, self.settings, self.media)
        self.assertEqual(result, ("good", "Title", MediaKind.VIDEO, 1280))
        self.context.bot.delete_message.assert_awaited_once()

    async def test_metadata_log_contains_operation_and_safe_dimensions(self) -> None:
        trace = _SendTrace("op-test", "direct", MediaFormat.VIDEO, None, 42)
        with self.assertLogs("telegram_share_bot.handlers", level="INFO") as logs:
            await _send_media_to_chat(self.context, 1, self.media, self.settings, trace=trace)
        text = "\n".join(logs.output)
        self.assertIn("op=op-test", text)
        self.assertIn("stage=video_metadata", text)
        self.assertIn("local_width=720 local_height=1280", text)
        self.assertIn("telegram_width=720 telegram_height=1280", text)
        self.assertNotIn(self.settings.bot_token, text)
        self.assertNotIn("Title", text)

    async def test_direct_bad_geometry_gets_feedback_and_no_cache_entry(self) -> None:
        self.context.bot.send_video.return_value = _message(MediaKind.VIDEO, 720, 720)
        status = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        with patch(
            "telegram_share_bot.handlers.direct.download_media", AsyncMock(return_value=self.media)
        ):
            await _run_direct_download(
                self.context,
                chat_id=42,
                user_id=42,
                url=self.url,
                custom_caption=None,
                time_range=None,
                status_message=status,
                preferences=self.preferences,
            )
        self.assertEqual(
            status.edit_text.await_args.args[0], strings.TELEGRAM_VIDEO_METADATA_FAILED
        )
        self.assertIsNone(await self.cache.get_preferred_video(self.url))
        self.context.bot.send_video.assert_awaited_once()

    async def test_bad_cached_geometry_evicts_and_refreshes_once(self) -> None:
        await self.cache.set(
            self.url,
            "bad",
            MediaKind.VIDEO,
            "Title",
            12,
            quality_policy="auto-best",
            video_width=720,
            video_height=1280,
        )
        self.context.bot.send_video.side_effect = [
            _message(MediaKind.VIDEO, 720, 720, "bad"),
            _message(MediaKind.VIDEO),
        ]
        status = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        download = AsyncMock(return_value=self.media)
        with patch("telegram_share_bot.handlers.direct.download_media", download):
            await _run_direct_download(
                self.context,
                chat_id=42,
                user_id=42,
                url=self.url,
                custom_caption=None,
                time_range=None,
                status_message=status,
                preferences=self.preferences,
            )
        download.assert_awaited_once()
        self.assertEqual(self.context.bot.send_video.await_count, 2)
        cached = await self.cache.get_preferred_video(self.url)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.file_id, "good")
        self.assertEqual((cached.video_width, cached.video_height), (720, 1280))

    async def test_inline_bad_metadata_preserves_retry_and_does_not_cache(self) -> None:
        self.context.bot.send_video.return_value = _message(MediaKind.VIDEO, 720, 720)
        with patch(
            "telegram_share_bot.handlers.prepare.download_media", AsyncMock(return_value=self.media)
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-test",
                user_id=42,
                result_id="result-test",
                url=self.url,
                preferences=self.preferences,
            )
        self.context.bot.edit_message_media.assert_not_awaited()
        error = self.context.bot.edit_message_text.await_args.kwargs
        self.assertEqual(error["text"], strings.TELEGRAM_VIDEO_METADATA_FAILED)
        self.assertEqual(
            error["reply_markup"].inline_keyboard[0][0].text, strings.INLINE_RETRY_BUTTON
        )
        self.assertIsNone(await self.cache.get_preferred_video(self.url))

    async def test_fresh_then_cached_inline_keeps_identical_geometry(self) -> None:
        download = AsyncMock(return_value=self.media)
        with patch("telegram_share_bot.handlers.prepare.download_media", download):
            for inline_id in ("first", "second"):
                await _prepare_inline_media(
                    self.context,
                    inline_message_id=inline_id,
                    user_id=42,
                    result_id=inline_id,
                    url=self.url,
                    preferences=self.preferences,
                )
                sent = self.context.bot.edit_message_media.await_args.kwargs["media"]
                self.assertEqual((sent.width, sent.height), (720, 1280))
        download.assert_awaited_once()
        self.context.bot.send_video.assert_awaited_once()
