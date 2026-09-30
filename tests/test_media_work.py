"""Tests for admission limits and worker-lifetime ownership."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot import strings
from telegram_share_bot.media.direct import get_direct_stream
from telegram_share_bot.media.files import _cleanup_dir
from telegram_share_bot.media.jobs import (
    _remember_active_download_dir,
    cleanup_stale_downloads,
    download_media,
)
from telegram_share_bot.media.models import DownloadError
from telegram_share_bot.media.work import MediaWorkSupervisor


class TestMediaWorkSupervisor(unittest.IsolatedAsyncioTestCase):
    async def test_try_acquire_falls_back_without_joining_full_queue(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        first = await supervisor.acquire(2)

        self.assertIsNone(await supervisor.try_acquire(2))
        await first.release()
        available = await supervisor.try_acquire(2)
        self.assertIsNotNone(available)
        assert available is not None
        await available.release()

    async def test_direct_url_preflight_runs_off_the_event_loop(self) -> None:
        event_loop_thread = threading.get_ident()
        validation_threads: list[int] = []

        def reject_url(*_args: object, **_kwargs: object) -> bool:
            validation_threads.append(threading.get_ident())
            return False

        with patch(
            "telegram_share_bot.media.direct.is_safe_media_url",
            side_effect=reject_url,
        ):
            result = await get_direct_stream(
                "https://example.com/video", 1024, timeout_seconds=1
            )

        self.assertIsNone(result)
        self.assertEqual(len(validation_threads), 1)
        self.assertNotEqual(validation_threads[0], event_loop_thread)

    async def test_download_url_preflight_runs_off_the_event_loop(self) -> None:
        event_loop_thread = threading.get_ident()
        validation_threads: list[int] = []

        def reject_url(*_args: object, **_kwargs: object) -> bool:
            validation_threads.append(threading.get_ident())
            return False

        with (
            patch(
                "telegram_share_bot.media.transfer.is_safe_media_url",
                side_effect=reject_url,
            ),
            self.assertRaises(DownloadError),
        ):
            await download_media(
                "https://example.com/video",
                Path("downloads"),
                1024,
                1,
            )

        self.assertEqual(len(validation_threads), 1)
        self.assertNotEqual(validation_threads[0], event_loop_thread)

    async def test_timed_out_worker_keeps_global_capacity_until_thread_exits(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        lease = await supervisor.acquire(2)
        started = threading.Event()
        finish = threading.Event()
        abort = threading.Event()

        def blocking_work() -> None:
            started.set()
            finish.wait(2)

        with self.assertRaises(TimeoutError):
            await lease.run_sync(
                blocking_work,
                wait_timeout_seconds=0.05,
                abort_event=abort,
            )
        self.assertTrue(started.is_set())
        self.assertTrue(abort.is_set())
        await lease.release()

        waiter = asyncio.create_task(supervisor.acquire(1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        finish.set()
        next_lease = await waiter
        await next_lease.release()

    async def test_cancelled_caller_does_not_cancel_or_free_worker(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        lease = await supervisor.acquire(2)
        started = threading.Event()
        finish = threading.Event()
        abort = threading.Event()

        def blocking_work() -> None:
            started.set()
            finish.wait(2)

        caller = asyncio.create_task(
            lease.run_sync(
                blocking_work,
                wait_timeout_seconds=2,
                abort_event=abort,
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(abort.is_set())
        await lease.release()

        waiter = asyncio.create_task(supervisor.acquire(1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        finish.set()
        next_lease = await waiter
        await next_lease.release()

    async def test_waiter_limit_and_cancelled_waiter_recover_capacity(self) -> None:
        supervisor = MediaWorkSupervisor(1, max_waiters=1)
        first = await supervisor.acquire(2)
        waiter = asyncio.create_task(supervisor.acquire(2))
        await asyncio.sleep(0)

        with self.assertRaises(DownloadError) as caught:
            await supervisor.acquire(2)
        self.assertEqual(str(caught.exception), strings.DOWNLOAD_BUSY)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await first.release()
        recovered = await supervisor.acquire(1)
        await recovered.release()

    async def test_queue_wait_has_a_separate_finite_bound(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        first = await supervisor.acquire(2)
        with patch("telegram_share_bot.media.work._MAX_ADMISSION_WAIT_SECONDS", 0.01):
            waiter = asyncio.create_task(supervisor.acquire(2))
            await asyncio.sleep(0)
            with self.assertRaises(DownloadError) as caught:
                await waiter
        self.assertEqual(str(caught.exception), strings.DOWNLOAD_BUSY)
        await first.release()

    async def test_default_waiter_budget_tracks_execution_capacity(self) -> None:
        supervisor = MediaWorkSupervisor(6)
        self.assertEqual(supervisor._max_waiters, 24)

    async def test_unlimited_configuration_remains_unlimited(self) -> None:
        supervisor = MediaWorkSupervisor(0)
        leases = [await supervisor.acquire(1) for _ in range(40)]
        for lease in leases:
            await lease.release()

    async def test_release_without_workers_returns_capacity_immediately(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        lease = await supervisor.acquire(1)
        await lease.release()
        recovered = await supervisor.acquire(1)
        await recovered.release()

    async def test_download_timeout_does_not_release_supervised_worker_slot(self) -> None:
        supervisor = MediaWorkSupervisor(1)
        lease = await supervisor.acquire(1)
        started = threading.Event()
        finish = threading.Event()

        def slow_download(*_args: object, **_kwargs: object) -> object:
            started.set()
            finish.wait(2)
            return object()

        with (
            patch("telegram_share_bot.media.jobs._download_sync", side_effect=slow_download),
            self.assertRaises(DownloadError),
        ):
            await download_media(
                "https://example.com/video",
                Path("downloads"),
                1000,
                0.05,
                work_lease=lease,
            )
        self.assertTrue(started.is_set())
        await lease.release()
        waiter = asyncio.create_task(supervisor.acquire(1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        finish.set()
        recovered = await waiter
        await recovered.release()


class TestDownloadDirectoryCleanup(unittest.TestCase):
    def test_locked_file_cleanup_is_best_effort_and_observable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / uuid.uuid4().hex
            directory.mkdir()
            locked = directory / "partial.mp4.part"
            locked.write_bytes(b"partial")

            with (
                patch.object(Path, "unlink", side_effect=PermissionError("locked")),
                self.assertLogs("telegram_share_bot.media.files", level="WARNING") as logs,
            ):
                _cleanup_dir(directory)

            self.assertTrue(locked.exists())
            self.assertTrue(any("locked or inaccessible" in line for line in logs.output))

    def test_sweeper_removes_only_old_uuid_directories_and_keeps_active_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "downloads"
            root.mkdir()
            stale = root / uuid.uuid4().hex
            stale.mkdir()
            (stale / "partial.mp4.part").write_bytes(b"partial")
            os.utime(stale, (time.time() - 7200, time.time() - 7200))
            active = root / uuid.uuid4().hex
            active.mkdir()
            os.utime(active, (time.time() - 7200, time.time() - 7200))
            unknown = root / "user-data"
            unknown.mkdir()
            (root / "cache.sqlite3").write_bytes(b"cache")

            _remember_active_download_dir(active, True)
            try:
                self.assertEqual(cleanup_stale_downloads(root), 1)
                self.assertFalse(stale.exists())
                self.assertTrue(active.exists())
                self.assertTrue(unknown.exists())
                self.assertTrue((root / "cache.sqlite3").exists())
            finally:
                _remember_active_download_dir(active, False)

            self.assertEqual(cleanup_stale_downloads(root), 1)
            self.assertFalse(active.exists())

    def test_sweeper_ignores_symlinked_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "downloads"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "keep.txt").write_text("keep")
            link = root / uuid.uuid4().hex
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            os.utime(link, (time.time() - 7200, time.time() - 7200))

            self.assertEqual(cleanup_stale_downloads(root), 0)
            self.assertTrue(link.is_symlink())
            self.assertTrue((outside / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
