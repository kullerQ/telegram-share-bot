"""Ownership and cleanup tests for inline request generations."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import NetworkError

from telegram_share_bot import strings
from telegram_share_bot.config import Settings
from telegram_share_bot.handlers import cancel_callback, retry_inline_callback
from telegram_share_bot.handlers.prepare import _prepare_inline_media
from telegram_share_bot.handlers.state import (
    _FINALIZER_TASKS,
    _active_inline_map,
    _attach_inline_task,
    _pending_map,
    _release_user_download_slot,
    _reserve_inline_request,
    _store_pending_url,
    _try_acquire_user_download_slot,
    _user_download_counts,
)
from telegram_share_bot.media.models import DownloadedMedia, MediaKind, TimeRange
from telegram_share_bot.storage.media_cache import MediaCache


class TestInlineRequestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cache.db"
        self.cache = MediaCache(self.db_path)
        settings = Settings(
            bot_token="test:token",
            storage_chat_id=123,
            max_file_bytes=1024,
            download_timeout_seconds=10,
            download_dir=Path(self.temp_dir.name) / "downloads",
            cache_db_path=self.db_path,
            delete_storage_messages=False,
            allow_public=True,
            max_downloads_per_user=3,
            download_cooldown_seconds=0,
        )
        self.context = MagicMock()
        self.context.application.bot_data = {
            "settings": settings,
            "media_cache": self.cache,
            "pending_inline": {},
            "inline_prepare_tasks": {},
            "active_inline_requests": {},
            "cancelled_inline": set(),
        }
        self.context.bot.edit_message_text = AsyncMock()

    async def asyncTearDown(self) -> None:
        for task in tuple(_FINALIZER_TASKS):
            await asyncio.gather(task, return_exceptions=True)
        self.temp_dir.cleanup()

    async def _drain_finalizers(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
            tasks = tuple(_FINALIZER_TASKS)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cancelled_before_coroutine_entry_releases_lease(self) -> None:
        active, denial = await _reserve_inline_request(
            self.context,
            inline_message_id="inline-1",
            result_id="result-1",
            user_id=42,
        )
        self.assertIsNone(denial)
        assert active is not None
        started = False

        async def work() -> None:
            nonlocal started
            started = True

        task = asyncio.create_task(work())
        _attach_inline_task(self.context, "inline-1", active, task)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._drain_finalizers()

        self.assertFalse(started)
        self.assertNotIn("inline-1", _active_inline_map(self.context))
        self.assertNotIn(42, _user_download_counts(self.context))

    async def test_other_allowed_user_cannot_cancel_active_generation(self) -> None:
        active, denial = await _reserve_inline_request(
            self.context,
            inline_message_id="inline-1",
            result_id="result-current",
            user_id=42,
        )
        self.assertIsNone(denial)
        assert active is not None
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(work())
        _attach_inline_task(self.context, "inline-1", active, task)
        await started.wait()

        query = MagicMock()
        query.data = "cancel:result-current"
        query.inline_message_id = "inline-1"
        query.from_user = MagicMock(id=7)
        query.answer = AsyncMock()
        await cancel_callback(MagicMock(callback_query=query), self.context)

        query.answer.assert_awaited_once_with(text=strings.SETTINGS_NOT_YOURS, show_alert=True)
        self.context.bot.edit_message_text.assert_not_awaited()
        self.assertFalse(task.cancelled())

        query.from_user.id = 42
        await cancel_callback(MagicMock(callback_query=query), self.context)
        query.answer.assert_awaited_with(text=strings.INLINE_CANCEL_ANSWER)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._drain_finalizers()
        self.assertNotIn(42, _user_download_counts(self.context))

    async def test_stale_cancel_token_cannot_cancel_replacement_generation(self) -> None:
        active, denial = await _reserve_inline_request(
            self.context,
            inline_message_id="inline-1",
            result_id="result-new",
            user_id=42,
        )
        self.assertIsNone(denial)
        assert active is not None
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(work())
        _attach_inline_task(self.context, "inline-1", active, task)
        await started.wait()

        query = MagicMock()
        query.data = "cancel:result-old"
        query.inline_message_id = "inline-1"
        query.from_user = MagicMock(id=42)
        query.answer = AsyncMock()
        await cancel_callback(MagicMock(callback_query=query), self.context)

        query.answer.assert_awaited_once_with(text=strings.INLINE_RETRY_EXPIRED, show_alert=True)
        self.assertFalse(task.cancelled())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._drain_finalizers()

    async def test_retry_answer_failure_restores_pending_and_releases_reservation(self) -> None:
        _store_pending_url(
            self.context,
            "result-old",
            "https://youtube.com/watch?v=example",
            time_range=TimeRange(60, 120),
            owner_user_id=42,
        )
        pending = _pending_map(self.context)["result-old"]
        query = MagicMock()
        query.data = "retry:result-old"
        query.inline_message_id = "inline-1"
        query.from_user = MagicMock(id=42)
        query.answer = AsyncMock(side_effect=RuntimeError("answer failed"))

        with self.assertRaisesRegex(RuntimeError, "answer failed"):
            await retry_inline_callback(MagicMock(callback_query=query), self.context)

        self.assertEqual(_pending_map(self.context)["result-old"], pending)
        self.assertFalse(_active_inline_map(self.context))
        self.assertNotIn(42, _user_download_counts(self.context))

    async def test_same_message_retries_reserve_once_before_telegram_wait(self) -> None:
        _store_pending_url(
            self.context,
            "result-old",
            "https://youtube.com/watch?v=example",
            owner_user_id=42,
        )
        answer_started = asyncio.Event()
        allow_answer = asyncio.Event()

        async def delayed_answer(**_kwargs: object) -> None:
            answer_started.set()
            await allow_answer.wait()

        first_query = MagicMock()
        first_query.data = "retry:result-old"
        first_query.inline_message_id = "inline-1"
        first_query.from_user = MagicMock(id=42)
        first_query.answer = AsyncMock(side_effect=delayed_answer)
        second_query = MagicMock()
        second_query.data = "retry:result-old"
        second_query.inline_message_id = "inline-1"
        second_query.from_user = MagicMock(id=42)
        second_query.answer = AsyncMock()

        with patch(
            "telegram_share_bot.handlers.inline._prepare_inline_media",
            new_callable=AsyncMock,
        ) as prepare:
            first = asyncio.create_task(
                retry_inline_callback(MagicMock(callback_query=first_query), self.context)
            )
            await asyncio.wait_for(answer_started.wait(), timeout=1)
            await retry_inline_callback(
                MagicMock(callback_query=second_query), self.context
            )
            second_query.answer.assert_awaited_once_with(
                text=strings.INLINE_ALREADY_PREPARING,
                show_alert=True,
            )
            allow_answer.set()
            await first
            task = self.context.application.bot_data["inline_prepare_tasks"]["inline-1"]
            await task
            await self._drain_finalizers()

        prepare.assert_awaited_once()
        self.assertNotIn(42, _user_download_counts(self.context))

    async def test_cleanup_failure_does_not_mask_retry_or_slot_release(self) -> None:
        active, denial = await _reserve_inline_request(
            self.context,
            inline_message_id="inline-1",
            result_id="result-1",
            user_id=42,
        )
        self.assertIsNone(denial)
        assert active is not None
        media = DownloadedMedia(
            path=Path(self.temp_dir.name) / "prepared.mp4",
            title="Prepared",
            kind=MediaKind.VIDEO,
            duration=5,
        )

        with (
            patch(
                "telegram_share_bot.handlers.prepare.download_media",
                new=AsyncMock(return_value=media),
            ),
            patch(
                "telegram_share_bot.handlers.prepare._upload_for_file_id",
                new=AsyncMock(side_effect=NetworkError("upload failed")),
            ),
            patch(
                "telegram_share_bot.handlers.prepare.cleanup_media",
                side_effect=OSError("file remained busy"),
            ),
        ):
            await _prepare_inline_media(
                self.context,
                inline_message_id="inline-1",
                url="https://youtube.com/watch?v=example",
                result_id="result-1",
                user_id=42,
                active_request=active,
            )

        self.assertIn("result-1", _pending_map(self.context))
        self.assertNotIn("inline-1", _active_inline_map(self.context))
        self.assertNotIn(42, _user_download_counts(self.context))

    async def test_old_cooldowns_and_request_timestamps_are_pruned(self) -> None:
        bot_data = self.context.application.bot_data
        bot_data["user_download_cooldowns"] = {7: 1.0}
        bot_data["user_download_request_times"] = {7: deque([1.0])}

        with patch("telegram_share_bot.handlers.state.time.monotonic", return_value=1000.0):
            self.assertIsNone(await _try_acquire_user_download_slot(self.context, 42))

        self.assertNotIn(7, bot_data["user_download_cooldowns"])
        self.assertNotIn(7, bot_data["user_download_request_times"])
        await _release_user_download_slot(self.context, 42)
