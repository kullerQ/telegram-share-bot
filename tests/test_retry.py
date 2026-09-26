"""Contextual retry behavior for transient inline delivery failures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import NetworkError

from telegram_share_bot.cache import MediaCache
from telegram_share_bot.config import Settings
from telegram_share_bot.downloader import (
    DirectMediaStream,
    DownloadError,
    MediaFormat,
    MediaKind,
    TimeRange,
)
from telegram_share_bot.handlers import (
    PendingInline,
    _prepare_inline_media,
    retry_inline_callback,
)


class TestInlineRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "retry.db"
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
        self.context.bot.edit_message_text = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_transient_clip_failure_offers_retry_and_full_video(self) -> None:
        time_range = TimeRange(60, 120)
        with patch(
            "telegram_share_bot.handlers.download_media",
            new=AsyncMock(
                side_effect=DownloadError("Download timed out after 30 seconds.")
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-clip",
                url="https://youtube.com/watch?v=example",
                result_id="clip-result",
                user_id=42,
                time_range=time_range,
            )

        markup = self.context.bot.edit_message_text.await_args.kwargs["reply_markup"]
        buttons = markup.inline_keyboard[0]
        self.assertEqual([button.text for button in buttons], ["Retry clip", "Send full video"])
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["retry:clip-result", "fallback:clip-result"],
        )
        self.assertTrue(
            self.context.application.bot_data["pending_inline"]["clip-result"]
        )

    async def test_network_upload_failure_offers_retry(self) -> None:
        stream = DirectMediaStream(
            direct_url="https://cdn.example/audio.m4a",
            title="Example",
            kind=MediaKind.AUDIO,
            duration=30,
        )
        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=stream),
            ),
            patch(
                "telegram_share_bot.handlers._upload_direct_url_for_file_id",
                new=AsyncMock(side_effect=NetworkError("temporary network issue")),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-network",
                url="https://example.com/video",
                result_id="network-result",
                user_id=42,
                media_format=MediaFormat.AUDIO,
            )

        markup = self.context.bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "retry:network-result")
        self.assertIn("network-result", self.context.application.bot_data["pending_inline"])

    async def test_permanent_download_failure_clears_cancel_button(self) -> None:
        with (
            patch(
                "telegram_share_bot.handlers.get_direct_stream",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "telegram_share_bot.handlers.download_media",
                new=AsyncMock(
                    side_effect=DownloadError(
                        "Playlist/empty result is not supported."
                    )
                ),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-permanent",
                url="https://youtube.com/watch?v=example",
                result_id="permanent-result",
                user_id=42,
            )

        markup = self.context.bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertFalse(markup.inline_keyboard)
        self.assertNotIn(
            "permanent-result", self.context.application.bot_data["pending_inline"]
        )

    async def test_full_video_action_retries_without_clip_range(self) -> None:
        result_id = "clip-result"
        self.context.application.bot_data["pending_inline"] = {
            result_id: PendingInline(
                url="https://youtube.com/watch?v=example",
                custom_caption="caption",
                time_range=TimeRange(60, 120),
            )
        }
        update = MagicMock()
        query = MagicMock()
        query.data = f"fallback:{result_id}"
        query.inline_message_id = "inline-clip"
        query.from_user.id = 42
        query.answer = AsyncMock()
        update.callback_query = query

        with patch(
            "telegram_share_bot.handlers._prepare_inline_media",
            new_callable=AsyncMock,
        ) as prepare:
            await retry_inline_callback(update, self.context)
            task = self.context.application.bot_data["inline_prepare_tasks"][
                "inline-clip"
            ]
            await task

        prepare.assert_awaited_once()
        self.assertIsNone(prepare.await_args.kwargs["time_range"])
        self.assertEqual(
            self.context.application.bot_data["pending_inline"][result_id].time_range,
            None,
        )
        query.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
