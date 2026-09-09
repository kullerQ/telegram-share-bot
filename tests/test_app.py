"""Unit tests for application construction and startup resiliency."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sendmedia_bot.app import _post_init, build_application


class TestAppInitialization(unittest.TestCase):
    def test_build_application(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_settings = MagicMock(
                bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
                cache_db_path=Path(tmp_dir) / "test.db",
                upload_timeout_seconds=180,
            )
            with patch("sendmedia_bot.app.load_settings", return_value=mock_settings):
                app = build_application()
                self.assertIsNotNone(app)
                self.assertEqual(app.bot_data["settings"], mock_settings)
                self.assertIsNotNone(app.post_init)
                self.assertEqual(app.bot.request._media_write_timeout, 180.0)

    async def _async_test_post_init_fetches_when_none(self) -> None:
        mock_app = MagicMock()
        mock_app.bot._bot_user = None
        mock_app.bot.get_me = AsyncMock()

        await _post_init(mock_app)
        mock_app.bot.get_me.assert_awaited_once()

    async def _async_test_post_init_skips_when_present(self) -> None:
        mock_app = MagicMock()
        mock_app.bot._bot_user = MagicMock()
        mock_app.bot.get_me = AsyncMock()

        await _post_init(mock_app)
        mock_app.bot.get_me.assert_not_awaited()

    def test_post_init_fetches_when_none(self) -> None:
        import asyncio
        asyncio.run(self._async_test_post_init_fetches_when_none())

    def test_post_init_skips_when_present(self) -> None:
        import asyncio
        asyncio.run(self._async_test_post_init_skips_when_present())
