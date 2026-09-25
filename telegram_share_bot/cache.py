"""SQLite persistent media cache for Telegram file_ids."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from telegram_share_bot.downloader import MediaFormat, MediaKind, TimeRange
from telegram_share_bot.normalizer import is_public_cacheable_url, normalize_url, safe_url_for_log

logger = logging.getLogger(__name__)

# Errors that typically mean the DB file/schema vanished or is unusable.
_RECOVERABLE_DB_ERRORS = (
    sqlite3.OperationalError,
    sqlite3.DatabaseError,
)


def _legacy_cache_key(url: str, time_range: TimeRange | None = None) -> str | None:
    """Key format used before requested format and quality were cache dimensions."""
    norm_url = normalize_url(url)
    if not norm_url:
        return None
    if time_range is None:
        return norm_url
    return f"{norm_url}{time_range.cache_suffix()}"


def _cache_key(
    url: str,
    time_range: TimeRange | None = None,
    *,
    media_format: MediaFormat = MediaFormat.VIDEO,
    quality_policy: str = "best-fit",
) -> str | None:
    """Key cached Telegram files by clip range, requested format, and quality."""
    legacy_key = _legacy_cache_key(url, time_range)
    if legacy_key is None:
        return None
    quality = quality_policy.strip().lower()
    if not quality or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in quality):
        quality = "best-fit"
    norm_url = normalize_url(url)
    if not norm_url:
        return None
    clip_suffix = time_range.cache_suffix() if time_range is not None else ""
    return f"{norm_url}#format={media_format.value}&quality={quality}{clip_suffix}"


@dataclass(frozen=True, slots=True)
class CachedMedia:
    url: str
    file_id: str
    kind: MediaKind
    title: str
    duration: int | None


class MediaCache:
    """Thread-safe, asynchronous SQLite cache for Telegram media file_ids."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._init_db()

    @contextlib.contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_cache (
                        url TEXT PRIMARY KEY,
                        file_id TEXT NOT NULL,
                        media_kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        duration INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_media_cache_last_used
                    ON media_cache(last_used_at);
                    """
                )
        finally:
            conn.close()

    def _recover_db(self, exc: BaseException) -> bool:
        """Recreate the DB schema after the file was deleted or became unusable.

        Returns True if re-init succeeded. Concurrent recoveries are serialized.
        """
        logger.warning(
            "Media cache DB unusable at %s (%s); recreating",
            self.db_path,
            exc,
        )
        with self._init_lock:
            try:
                # Drop leftover WAL/SHM/journal sidecars that can confuse a fresh file.
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(f"{self.db_path}{suffix}")
                    with contextlib.suppress(OSError):
                        sidecar.unlink(missing_ok=True)
                self._init_db()
                return True
            except Exception as recover_exc:
                logger.error(
                    "Failed to recreate media cache DB at %s: %s",
                    self.db_path,
                    recover_exc,
                )
                return False

    def _get_sync(self, norm_url: str) -> CachedMedia | None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT file_id, media_kind, title, duration
                FROM media_cache
                WHERE url = ?
                """,
                (norm_url,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            # Update last_used_at timestamp on access
            conn.execute(
                """
                UPDATE media_cache
                SET last_used_at = CURRENT_TIMESTAMP
                WHERE url = ?
                """,
                (norm_url,),
            )
            return CachedMedia(
                url=norm_url,
                file_id=row[0],
                kind=MediaKind(row[1]),
                title=row[2],
                duration=row[3],
            )

    def _set_sync(
        self,
        norm_url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO media_cache (url, file_id, media_kind, title, duration, last_used_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(url) DO UPDATE SET
                    file_id = excluded.file_id,
                    media_kind = excluded.media_kind,
                    title = excluded.title,
                    duration = excluded.duration,
                    last_used_at = CURRENT_TIMESTAMP
                """,
                (norm_url, file_id, kind.value, title, duration),
            )

    def _evict_sync(self, norm_url: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM media_cache WHERE url = ?", (norm_url,))
            return cursor.rowcount > 0

    def _get_with_recovery(self, norm_url: str) -> CachedMedia | None:
        try:
            return self._get_sync(norm_url)
        except _RECOVERABLE_DB_ERRORS as exc:
            if not self._recover_db(exc):
                raise
            return self._get_sync(norm_url)

    def _set_with_recovery(
        self,
        norm_url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
    ) -> None:
        try:
            self._set_sync(norm_url, file_id, kind, title, duration)
        except _RECOVERABLE_DB_ERRORS as exc:
            if not self._recover_db(exc):
                raise
            self._set_sync(norm_url, file_id, kind, title, duration)

    def _evict_with_recovery(self, norm_url: str) -> bool:
        try:
            return self._evict_sync(norm_url)
        except _RECOVERABLE_DB_ERRORS as exc:
            if not self._recover_db(exc):
                raise
            return self._evict_sync(norm_url)

    async def get(
        self,
        url: str,
        *,
        time_range: TimeRange | None = None,
        media_format: MediaFormat = MediaFormat.VIDEO,
        quality_policy: str = "best-fit",
    ) -> CachedMedia | None:
        """Fetch cached media by raw or normalized URL.

        If the DB file was deleted mid-run, recreates it and returns a cache miss.
        """
        if not is_public_cacheable_url(url):
            return None
        key = _cache_key(
            url,
            time_range,
            media_format=media_format,
            quality_policy=quality_policy,
        )
        if not key:
            return None
        try:
            cached = await asyncio.to_thread(self._get_with_recovery, key)
            if cached is not None:
                return cached
            if media_format is MediaFormat.VIDEO and quality_policy == "best-fit":
                legacy_key = _legacy_cache_key(url, time_range)
                if legacy_key is not None and legacy_key != key:
                    return await asyncio.to_thread(
                        self._get_with_recovery, legacy_key
                    )
            return None
        except _RECOVERABLE_DB_ERRORS:
            return None

    async def set(
        self,
        url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
        *,
        time_range: TimeRange | None = None,
        media_format: MediaFormat = MediaFormat.VIDEO,
        quality_policy: str = "best-fit",
    ) -> None:
        """Cache media file_id under the normalized URL.

        If the DB file was deleted mid-run, recreates it and retries once.
        Failures after recovery are logged and swallowed so downloads still succeed.
        """
        if not is_public_cacheable_url(url):
            logger.debug(
                "Skipping cache for non-public URL: %s", safe_url_for_log(url)
            )
            return
        key = _cache_key(
            url,
            time_range,
            media_format=media_format,
            quality_policy=quality_policy,
        )
        if not key:
            return
        try:
            await asyncio.to_thread(
                self._set_with_recovery, key, file_id, kind, title, duration
            )
            logger.debug("Cached file_id for %s (%s)", key, kind.value)
        except _RECOVERABLE_DB_ERRORS as exc:
            logger.warning(
                "Could not write media cache for %s: %s",
                safe_url_for_log(key),
                exc,
            )

    async def evict(
        self,
        url: str,
        *,
        time_range: TimeRange | None = None,
        media_format: MediaFormat = MediaFormat.VIDEO,
        quality_policy: str = "best-fit",
    ) -> None:
        """Evict a URL from the cache (e.g. if file_id is invalid).

        Missing/deleted DB is treated as already empty.
        """
        key = _cache_key(
            url,
            time_range,
            media_format=media_format,
            quality_policy=quality_policy,
        )
        if not key:
            return
        try:
            removed = await asyncio.to_thread(self._evict_with_recovery, key)
            if media_format is MediaFormat.VIDEO and quality_policy == "best-fit":
                legacy_key = _legacy_cache_key(url, time_range)
                if legacy_key is not None and legacy_key != key:
                    removed = (
                        await asyncio.to_thread(self._evict_with_recovery, legacy_key)
                    ) or removed
        except _RECOVERABLE_DB_ERRORS:
            return
        if removed:
            logger.info("Evicted %s from media cache", safe_url_for_log(key))
