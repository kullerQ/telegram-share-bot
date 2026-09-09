"""SQLite persistent media cache for Telegram file_ids."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from sendmedia_bot.downloader import MediaKind
from sendmedia_bot.normalizer import normalize_url

logger = logging.getLogger(__name__)


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
        self._init_db()

    @contextlib.contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
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

    async def get(self, url: str) -> CachedMedia | None:
        """Fetch cached media by raw or normalized URL."""
        norm_url = normalize_url(url)
        if not norm_url:
            return None
        return await asyncio.to_thread(self._get_sync, norm_url)

    async def set(
        self,
        url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
    ) -> None:
        """Cache media file_id under the normalized URL."""
        norm_url = normalize_url(url)
        if not norm_url:
            return
        await asyncio.to_thread(self._set_sync, norm_url, file_id, kind, title, duration)
        logger.debug("Cached file_id for %s (%s)", norm_url, kind.value)

    async def evict(self, url: str) -> None:
        """Evict a URL from the cache (e.g. if file_id is invalid)."""
        norm_url = normalize_url(url)
        if not norm_url:
            return
        removed = await asyncio.to_thread(self._evict_sync, norm_url)
        if removed:
            logger.info("Evicted %s from media cache", norm_url)
