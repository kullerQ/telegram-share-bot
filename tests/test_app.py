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
from telegram_share_bot.app import _post_init, _validate_storage_chat, build_application
from telegram_share_bot.config import Settings


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
                self.assertEqual(app.bot.request._media_write_timeout, 180.0)

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
