"""Persistence and caption behavior for per-user sharing settings."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram_share_bot.cache import CachedMedia
from telegram_share_bot.config import CaptionMode, Settings
from telegram_share_bot.downloader import (
    DownloadedMedia,
    MediaFormat,
    MediaKind,
    VideoQualityPolicy,
)
from telegram_share_bot.handlers import (
    _input_media,
    _resolve_user_caption,
    _send_cached_media_to_chat,
    _send_media_to_chat,
    _settings_keyboard,
    _settings_text,
    settings_callback,
)
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
        await self.store.set_caption(1001, CaptionPreference.CUSTOM)
        await self.store.set_format(1001, MediaFormat.AUDIO)
        await self.store.set_quality(2002, VideoQualityPolicy.BALANCED)

        reopened = UserSettingsStore(self.db_path)
        first = await reopened.get(1001)
        second = await reopened.get(2002)
        self.assertEqual(first.video_quality, VideoQualityPolicy.BEST)
        self.assertEqual(first.caption, CaptionPreference.CUSTOM)
        self.assertEqual(first.default_format, MediaFormat.AUDIO)
        self.assertEqual(second.video_quality, VideoQualityPolicy.BALANCED)
        self.assertIsNone(second.caption)
        self.assertIsNone(second.default_format)

    async def test_reset_restores_all_defaults(self) -> None:
        await self.store.set_quality(1001, VideoQualityPolicy.BEST)
        await self.store.set_caption(1001, CaptionPreference.CUSTOM)
        await self.store.set_format(1001, MediaFormat.VIDEO)
        reset = await self.store.reset(1001)
        self.assertEqual(reset.video_quality, VideoQualityPolicy.AUTO)
        self.assertIsNone(reset.caption)
        self.assertIsNone(reset.default_format)
        self.assertEqual(await self.store.get(1001), reset)

    async def test_previous_caption_values_remain_readable(self) -> None:
        await self.store.set_caption(1001, CaptionPreference.MEDIA_TITLE)
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE user_settings SET caption = 'original-link' WHERE user_id = 1001"
                )
        self.assertEqual((await self.store.get(1001)).caption, CaptionPreference.MEDIA_TITLE)
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE user_settings SET caption = 'none' WHERE user_id = 1001")
        self.assertEqual((await self.store.get(1001)).caption, CaptionPreference.CUSTOM)


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
        original = "https://example.com/video?id=abc&part=1"
        preferences = UserSharingSettings(caption=CaptionPreference.MEDIA_TITLE)
        title = _resolve_user_caption(
            preferences,
            self.settings,
            media_title="🐈 A title & more",
            original_url=original,
            custom_caption=None,
        )
        self.assertEqual(title.text, "🐈 A title & more")
        self.assertEqual(len(title.entities), 1)
        self.assertEqual(title.entities[0].url, original)
        self.assertEqual(title.entities[0].length, len(title.text.encode("utf-16-le")) // 2)
        media = _input_media("file_id", title.text, MediaKind.VIDEO, caption=title)
        self.assertEqual(media.caption, title.text)
        self.assertEqual(media.caption_entities, title.entities)

        override = _resolve_user_caption(
            preferences,
            self.settings,
            media_title="A title",
            original_url=original,
            custom_caption="Custom <override>",
        )
        self.assertEqual(override.text, "Custom <override>")
        self.assertEqual(override.entities, ())

    def test_custom_choice_needs_per_send_text(self) -> None:
        caption = _resolve_user_caption(
            UserSharingSettings(caption=CaptionPreference.CUSTOM),
            self.settings,
            media_title="A title",
            original_url="https://example.com/video",
            custom_caption=None,
        )
        self.assertIsNone(caption.text)
        self.assertEqual(caption.entities, ())

    def test_unsaved_caption_keeps_global_mode(self) -> None:
        self.assertIsNone(
            _resolve_user_caption(
                UserSharingSettings(),
                self.settings,
                media_title="A title",
                original_url="https://example.com/video",
                custom_caption=None,
            ).text
        )


class TestSettingsPresentation(unittest.TestCase):
    def test_lists_options_and_marks_selections_in_text_and_buttons(self) -> None:
        settings = Settings(
            bot_token="test:token",
            storage_chat_id=1,
            max_file_bytes=1024,
            download_timeout_seconds=10,
            download_dir=Path("downloads"),
            cache_db_path=Path("downloads/media_cache.db"),
            delete_storage_messages=True,
        )
        preferences = UserSharingSettings(
            video_quality=VideoQualityPolicy.BEST,
            caption=CaptionPreference.CUSTOM,
            default_format=MediaFormat.AUDIO,
        )
        text = _settings_text(preferences, settings)
        self.assertIn("✅ <b>Best</b>", text)
        self.assertIn("✅ <b>Custom</b>", text)
        self.assertIn("✅ <b>Audio</b>", text)
        self.assertIn("• Auto", text)
        self.assertIn("• Media title", text)
        self.assertIn("• Not specified", text)
        self.assertNotIn("720p", text)

        keyboard = _settings_keyboard(1001, preferences, settings).inline_keyboard
        self.assertEqual([button.text for button in keyboard[1]], ["✅ Custom", "Media title"])
        self.assertEqual(keyboard[2][0].text, "Not specified")
        self.assertEqual(keyboard[-1][0].text, "Reset to default")


class TestLinkedCaptionDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_and_cached_video_use_a_linked_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                bot_token="test:token",
                storage_chat_id=1,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=Path(temp_dir),
                cache_db_path=Path(temp_dir) / "media_cache.db",
                delete_storage_messages=True,
            )
            context = MagicMock()
            context.application.bot_data = {"settings": settings}
            context.bot.send_video = AsyncMock(return_value=MagicMock())
            url = "https://example.com/video?id=abc&part=1"
            preferences = UserSharingSettings(caption=CaptionPreference.MEDIA_TITLE)
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(b"media")

            await _send_media_to_chat(
                context,
                1001,
                DownloadedMedia(path, "A title", MediaKind.VIDEO, 12),
                settings=settings,
                preferences=preferences,
                original_url=url,
            )
            fresh = context.bot.send_video.await_args.kwargs
            self.assertEqual(fresh["caption"], "A title")
            self.assertEqual(fresh["caption_entities"][0].url, url)

            await _send_cached_media_to_chat(
                context,
                1001,
                CachedMedia(url, "file_id", MediaKind.VIDEO, "A title", 12),
                preferences=preferences,
                original_url=url,
            )
            cached = context.bot.send_video.await_args.kwargs
            self.assertEqual(cached["caption"], "A title")
            self.assertEqual(cached["caption_entities"][0].url, url)


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

    async def test_reset_callback_restores_default_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UserSettingsStore(Path(temp_dir) / "user_settings.db")
            await store.set_quality(1001, VideoQualityPolicy.BEST)
            await store.set_caption(1001, CaptionPreference.CUSTOM)
            await store.set_format(1001, MediaFormat.AUDIO)
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
            query.data = "settings:1001:all:reset"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()

            await settings_callback(MagicMock(callback_query=query), context)

            self.assertEqual(await store.get(1001), UserSharingSettings())
            self.assertIn("✅ <b>Auto</b>", query.edit_message_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
