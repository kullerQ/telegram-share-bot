"""Unit tests for application construction and startup resiliency."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.constants import ChatType
from telegram.error import BadRequest

from telegram_share_bot import strings
from telegram_share_bot.app import (
    _MEDIA_MAINTENANCE_STOP,
    _MEDIA_MAINTENANCE_TASK,
    _maintain_downloads,
    _maintain_media_cache,
    _post_init,
    _post_shutdown,
    _validate_storage_chat,
    build_application,
)
from telegram_share_bot.config import Settings
from telegram_share_bot.media.models import VideoQualityPolicy
from telegram_share_bot.media.work import MediaWorkSupervisor
from telegram_share_bot.platforms.previews import PreviewResolver
from telegram_share_bot.storage.media_cache import MediaCache
from telegram_share_bot.storage.user_settings import UserSettingsStore


class TestAppInitialization(unittest.TestCase):
    def test_build_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_settings = MagicMock(
                bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
                cache_db_path=Path(tmp_dir) / "test.db",
                upload_timeout_seconds=180,
            )
            with patch("telegram_share_bot.app.load_settings", return_value=mock_settings):
                app = build_application()
                self.assertIsNotNone(app)
                self.assertEqual(app.bot_data["settings"], mock_settings)
                self.assertIsNotNone(app.post_init)
                self.assertIsNotNone(app.post_shutdown)
                self.assertIsInstance(
                    app.bot_data["media_work_supervisor"], MediaWorkSupervisor
                )
                self.assertIsInstance(app.bot_data["preview_resolver"], PreviewResolver)
                self.assertEqual(app.bot.request._media_write_timeout, 180.0)

    def test_corrupt_optional_cache_does_not_reset_user_settings(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                settings_path = root / "data" / "user_settings.db"
                expected_store = UserSettingsStore(settings_path)
                expected = await expected_store.set_quality(42, VideoQualityPolicy.BEST)
                cache_path = root / "downloads" / "media_cache.db"
                cache_path.parent.mkdir(parents=True)
                cache_path.write_bytes(b"corrupt optional cache")
                settings = MagicMock(
                    bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
                    cache_db_path=cache_path,
                    user_settings_db_path=settings_path,
                    upload_timeout_seconds=180,
                )

                with patch("telegram_share_bot.app.load_settings", return_value=settings):
                    app = build_application()

                self.assertFalse(app.bot_data["media_cache"].available)
                self.assertEqual(await app.bot_data["user_settings"].get(42), expected)
                await _post_shutdown(app)

        asyncio.run(_run())

    def test_shutdown_closes_owned_preview_client(self) -> None:
        async def _run() -> None:
            resolver = PreviewResolver()
            await resolver.start()
            app = MagicMock()
            app.bot_data = {"preview_resolver": resolver}

            await _post_shutdown(app)

            self.assertIsNone(resolver._client)
            self.assertTrue(resolver._closed)

        asyncio.run(_run())

    def test_startup_opens_owned_preview_client(self) -> None:
        async def _run() -> None:
            resolver = PreviewResolver()
            app = MagicMock()
            app.bot._bot_user = MagicMock()
            app.bot_data = {"preview_resolver": resolver}

            await _post_init(app)
            self.assertIsNotNone(resolver._client)
            await _post_shutdown(app)

        asyncio.run(_run())

    def test_startup_runs_cache_maintenance(self) -> None:
        async def _run() -> None:
            cache = MagicMock(spec=MediaCache)
            cache.maintain = AsyncMock(return_value=(0, 0))
            app = MagicMock()
            app.bot._bot_user = MagicMock()
            app.bot_data = {"media_cache": cache}

            await _post_init(app)

            cache.maintain.assert_awaited_once_with()
            await _post_shutdown(app)

        asyncio.run(_run())

    def test_cache_maintenance_logs_counts_without_database_keys(self) -> None:
        async def _run() -> None:
            cache = MagicMock(spec=MediaCache)
            cache.maintain = AsyncMock(return_value=(3, 2))
            with self.assertLogs("telegram_share_bot.app", level="INFO") as captured:
                await _maintain_media_cache(cache)
            self.assertIn("3 least-recently-used rows", captured.output[0])
            self.assertIn("2 credential-bearing rows", captured.output[0])
            self.assertNotIn("secret-value", captured.output[0])

        asyncio.run(_run())

    def test_shutdown_stops_owned_download_maintenance_task(self) -> None:
        async def _run() -> None:
            stop_event = asyncio.Event()
            task = asyncio.create_task(_maintain_downloads(Path("."), stop_event))
            app = MagicMock()
            app.bot_data = {
                _MEDIA_MAINTENANCE_STOP: stop_event,
                _MEDIA_MAINTENANCE_TASK: task,
            }
            await _post_shutdown(app)
            self.assertTrue(task.done())

        asyncio.run(_run())

    async def _async_test_post_init_fetches_when_none(self) -> None:
        mock_app = MagicMock()
        mock_app.bot._bot_user = None
        mock_app.bot.get_me = AsyncMock()
        mock_app.bot_data = {}

        await _post_init(mock_app)
        mock_app.bot.get_me.assert_awaited_once()

    async def _async_test_post_init_skips_when_present(self) -> None:
        mock_app = MagicMock()
        mock_app.bot._bot_user = MagicMock()
        mock_app.bot.get_me = AsyncMock()
        mock_app.bot_data = {}

        await _post_init(mock_app)
        mock_app.bot.get_me.assert_not_awaited()

    def test_post_init_fetches_when_none(self) -> None:
        asyncio.run(self._async_test_post_init_fetches_when_none())

    def test_post_init_skips_when_present(self) -> None:
        asyncio.run(self._async_test_post_init_skips_when_present())

    def test_validate_storage_chat_rejects_group(self) -> None:
        async def _run() -> None:
            settings = Settings(
                bot_token="test:token",
                storage_chat_id=-1001,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=Path("."),
                cache_db_path=Path("cache.db"),
                delete_storage_messages=True,
            )
            app = MagicMock()
            app.bot.get_chat = AsyncMock(
                return_value=MagicMock(type=ChatType.SUPERGROUP)
            )
            with self.assertRaises(RuntimeError) as ctx:
                await _validate_storage_chat(app, settings)
            self.assertEqual(str(ctx.exception), strings.CONFIG_STORAGE_CHAT_NOT_PRIVATE)

        asyncio.run(_run())

    def test_validate_storage_chat_allows_private(self) -> None:
        async def _run() -> None:
            settings = Settings(
                bot_token="test:token",
                storage_chat_id=42,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=Path("."),
                cache_db_path=Path("cache.db"),
                delete_storage_messages=True,
            )
            app = MagicMock()
            app.bot.get_chat = AsyncMock(
                return_value=MagicMock(type=ChatType.PRIVATE)
            )
            await _validate_storage_chat(app, settings)

        asyncio.run(_run())

    def test_validate_storage_chat_unreachable(self) -> None:
        async def _run() -> None:
            settings = Settings(
                bot_token="test:token",
                storage_chat_id=42,
                max_file_bytes=1024,
                download_timeout_seconds=10,
                download_dir=Path("."),
                cache_db_path=Path("cache.db"),
                delete_storage_messages=True,
            )
            app = MagicMock()
            app.bot.get_chat = AsyncMock(side_effect=BadRequest("chat not found"))
            with self.assertRaises(RuntimeError) as ctx:
                await _validate_storage_chat(app, settings)
            self.assertEqual(str(ctx.exception), strings.CONFIG_STORAGE_CHAT_UNREACHABLE)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
