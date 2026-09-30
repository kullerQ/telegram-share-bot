"""Bounded admission and truthful lifetime tracking for blocking media work."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from telegram_share_bot import strings
from telegram_share_bot.media.models import DownloadError

logger = logging.getLogger(__name__)

_MAX_ADMISSION_WAIT_SECONDS = 30.0


class MediaWorkLease:
    """Own one global media slot until every submitted worker has really exited."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore | None,
        deadline: float,
    ) -> None:
        self._semaphore = semaphore
        self.deadline = deadline
        self._workers: dict[asyncio.Task[Any], bool] = {}
        self._release_requested = False
        self._released = False

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def _worker_finished(self, worker: asyncio.Task[Any]) -> None:
        abandoned = self._workers.pop(worker, False)
        if not worker.cancelled():
            try:
                error = worker.exception()
            except BaseException:
                error = None
            if error is not None and abandoned:
                logger.warning(
                    "Media worker failed after its request stopped waiting (%s)",
                    type(error).__name__,
                )
        self._release_capacity_if_ready()

    def _release_capacity_if_ready(self) -> None:
        if self._release_requested and not self._workers and not self._released:
            self._released = True
            if self._semaphore is not None:
                self._semaphore.release()

    def _register_worker(
        self,
        worker: asyncio.Task[Any],
        on_worker_registered: Callable[[asyncio.Task[Any]], None] | None,
    ) -> None:
        self._workers[worker] = False
        worker.add_done_callback(self._worker_finished)
        if on_worker_registered is not None:
            on_worker_registered(worker)

    async def run_sync(
        self,
        function: Callable[..., Any],
        *args: Any,
        wait_timeout_seconds: float,
        abort_event: threading.Event | None = None,
        on_worker_registered: Callable[[asyncio.Task[Any]], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run blocking code without treating a timed-out thread as stopped."""
        remaining = min(wait_timeout_seconds, self.remaining_seconds())
        if remaining <= 0:
            if abort_event is not None:
                abort_event.set()
            raise TimeoutError

        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._register_worker(worker, on_worker_registered)
        try:
            completed, _ = await asyncio.wait({worker}, timeout=remaining)
        except asyncio.CancelledError:
            self._workers[worker] = True
            if abort_event is not None:
                abort_event.set()
            raise
        if worker not in completed:
            self._workers[worker] = True
            if abort_event is not None:
                abort_event.set()
            raise TimeoutError
        return worker.result()

    async def release(self) -> None:
        """Request release; capacity stays held while any worker is still running."""
        self._release_requested = True
        self._release_capacity_if_ready()


class MediaWorkSupervisor:
    """Bound active media work, queued requests, and time spent waiting."""

    def __init__(
        self,
        concurrency: int,
        *,
        max_waiters: int | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        concurrency = max(0, concurrency)
        self._semaphore = semaphore if semaphore is not None else (
            asyncio.Semaphore(concurrency) if concurrency else None
        )
        # A zero-concurrency configuration explicitly disables admission control.
        self._max_waiters = (
            max(1, concurrency * 4)
            if max_waiters is None and concurrency
            else max(0, max_waiters or 0)
        )
        self._waiters = 0

    async def acquire(self, timeout_seconds: float) -> MediaWorkLease:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        semaphore = self._semaphore
        if semaphore is None:
            return MediaWorkLease(None, deadline)
        if timeout_seconds <= 0:
            raise DownloadError(strings.DOWNLOAD_TIMED_OUT.format(timeout_seconds=0))

        # These operations run on the same event loop and contain no await, so
        # admission and waiter accounting are atomic without another lock.
        if self._waiters >= self._max_waiters:
            raise DownloadError(strings.DOWNLOAD_BUSY, retryable=True)
        self._waiters += 1
        try:
            wait_seconds = min(_MAX_ADMISSION_WAIT_SECONDS, timeout_seconds)
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=wait_seconds)
            except TimeoutError as exc:
                raise DownloadError(strings.DOWNLOAD_BUSY, retryable=True) from exc
        finally:
            self._waiters -= 1
        return MediaWorkLease(semaphore, deadline)
