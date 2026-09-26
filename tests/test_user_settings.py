"""Persistence and caption behavior for per-user sharing settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram_share_bot.config import CaptionMode, Settings
from telegram_share_bot.downloader import MediaFormat, VideoQualityPolicy
from telegram_share_bot.handlers import _resolve_user_caption, settings_callback
from telegram_share_bot.user_settings import (
    CaptionPreference,
    UserSettingsStore,
    UserSharingSettings,
)


class TestUserSettingsStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "data" / "user_settings.db"
        self.store = UserSettingsStore(self.db_path)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_defaults_and_user_isolation_persist_across_reopen(self) -> None:
        first_default = await self.store.get(1001)
        self.assertEqual(first_default.video_quality, VideoQualityPolicy.AUTO)
        self.assertIsNone(first_default.caption)
        self.assertIsNone(first_default.default_format)

        await self.store.set_quality(1001, VideoQualityPolicy.BEST)
        await self.store.set_caption(1001, CaptionPreference.ORIGINAL_LINK)
        await self.store.set_format(1001, MediaFormat.AUDIO)
        await self.store.set_quality(2002, VideoQualityPolicy.BALANCED)

        reopened = UserSettingsStore(self.db_path)
        first = await reopened.get(1001)
        second = await reopened.get(2002)
        self.assertEqual(first.video_quality, VideoQualityPolicy.BEST)
        self.assertEqual(first.caption, CaptionPreference.ORIGINAL_LINK)
        self.assertEqual(first.default_format, MediaFormat.AUDIO)
        self.assertEqual(second.video_quality, VideoQualityPolicy.BALANCED)
        self.assertIsNone(second.caption)
        self.assertIsNone(second.default_format)

    async def test_caption_and_format_can_return_to_unspecified(self) -> None:
        await self.store.set_caption(1001, CaptionPreference.NONE)
        await self.store.set_format(1001, MediaFormat.VIDEO)
        await self.store.set_caption(1001, None)
        reset = await self.store.set_format(1001, None)
        self.assertIsNone(reset.caption)
        self.assertIsNone(reset.default_format)


class TestPerUserCaptionResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            bot_token="test:token",
            storage_chat_id=1,
            max_file_bytes=1024,
            download_timeout_seconds=10,
            download_dir=Path("downloads"),
            cache_db_path=Path("downloads/media_cache.db"),
            delete_storage_messages=True,
            caption_mode=CaptionMode.CUSTOM,
        )

    def test_saved_caption_and_explicit_caption_precedence(self) -> None:
        original = "https://example.com/video?id=abc"
        preferences = UserSharingSettings(caption=CaptionPreference.ORIGINAL_LINK)
        self.assertEqual(
            _resolve_user_caption(
                preferences,
                self.settings,
                media_title="A title",
                original_url=original,
                custom_caption=None,
            ),
            original,
        )
        self.assertEqual(
            _resolve_user_caption(
                preferences,
                self.settings,
                media_title="A title",
                original_url=original,
                custom_caption="Custom override",
            ),
            "Custom override",
        )

    def test_unsaved_caption_keeps_global_mode(self) -> None:
        self.assertIsNone(
            _resolve_user_caption(
                UserSharingSettings(),
                self.settings,
                media_title="A title",
                original_url="https://example.com/video",
                custom_caption=None,
            )
        )


class TestSettingsCallback(unittest.IsolatedAsyncioTestCase):
    async def test_valid_choice_updates_only_the_originating_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "user_settings.db"
            store = UserSettingsStore(db_path)
            context = MagicMock()
            context.application.bot_data = {
                "settings": Settings(
                    bot_token="test:token",
                    storage_chat_id=1,
                    max_file_bytes=1024,
                    download_timeout_seconds=10,
                    download_dir=Path(temp_dir),
                    cache_db_path=Path(temp_dir) / "media_cache.db",
                    delete_storage_messages=True,
                    allow_public=True,
                ),
                "user_settings": store,
            }
            query = MagicMock()
            query.from_user.id = 1001
            query.data = "settings:1001:quality:balanced"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            update = MagicMock(callback_query=query)

            await settings_callback(update, context)

            self.assertEqual(
                (await store.get(1001)).video_quality,
                VideoQualityPolicy.BALANCED,
            )
            self.assertEqual(
                (await store.get(2002)).video_quality,
                VideoQualityPolicy.AUTO,
            )
            query.edit_message_text.assert_awaited_once()

    async def test_rejects_a_callback_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UserSettingsStore(Path(temp_dir) / "user_settings.db")
            context = MagicMock()
            context.application.bot_data = {
                "settings": Settings(
                    bot_token="test:token",
                    storage_chat_id=1,
                    max_file_bytes=1024,
                    download_timeout_seconds=10,
                    download_dir=Path(temp_dir),
                    cache_db_path=Path(temp_dir) / "media_cache.db",
                    delete_storage_messages=True,
                    allow_public=True,
                ),
                "user_settings": store,
            }
            query = MagicMock()
            query.from_user.id = 2002
            query.data = "settings:1001:quality:best"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()

            await settings_callback(MagicMock(callback_query=query), context)

            query.edit_message_text.assert_not_awaited()
            self.assertEqual(
                (await store.get(1001)).video_quality,
                VideoQualityPolicy.AUTO,
            )


if __name__ == "__main__":
    unittest.main()
