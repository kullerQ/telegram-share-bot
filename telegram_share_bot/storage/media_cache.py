"""SQLite persistent media cache for Telegram file_ids."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from telegram_share_bot.media.models import MediaFormat, MediaKind, TimeRange
from telegram_share_bot.platforms.urls import (
    is_public_cacheable_url,
    normalize_url,
    safe_url_for_log,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_REQUIRED_CACHE_COLUMNS = {
    "url",
    "file_id",
    "media_kind",
    "title",
    "duration",
    "video_height",
    "last_used_at",
}
_CACHE_MAX_ENTRIES = 10_000
_CACHE_MAINTENANCE_BATCH_SIZE = 500


class _CacheUnavailable(RuntimeError):
    """An optional cache operation could not safely complete."""


class _CacheSchemaError(RuntimeError):
    """The cache database has a schema that cannot be migrated safely."""


def _sqlite_error_category(error: BaseException) -> str:
    """Classify SQLite failures without including SQL values or filesystem paths."""
    code = getattr(error, "sqlite_errorname", "")
    if code.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return "busy"
    if code in {"SQLITE_PERM", "SQLITE_CANTOPEN", "SQLITE_READONLY"}:
        return "access"
    if code.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_FORMAT")):
        return "corrupt"

    message = str(error).casefold()
    if "database is locked" in message or "database is busy" in message:
        return "busy"
    if "permission denied" in message or "readonly database" in message:
        return "access"
    if "unable to open database file" in message or "disk i/o error" in message:
        return "storage"
    if "file is not a database" in message or "database disk image is malformed" in message:
        return "corrupt"
    if "no such table: media_cache" in message:
        return "missing_schema"
    if isinstance(error, _CacheSchemaError):
        return "incompatible_schema"
    return "database"


def _cache_key_has_userinfo(key: str) -> bool:
    """Detect credentials in a historical normalized URL without exposing them."""
    source_url = key.split("#format=", 1)[0]
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return False
    return parsed.username is not None or parsed.password is not None


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
    # Earlier video entries could contain an unchecked audio component or codec.
    delivery = "&delivery=2" if media_format is MediaFormat.VIDEO else ""
    return f"{norm_url}#format={media_format.value}&quality={quality}{delivery}{clip_suffix}"


@dataclass(frozen=True, slots=True)
class CachedMedia:
    url: str
    file_id: str
    kind: MediaKind
    title: str
    duration: int | None
    video_height: int | None = None


class MediaCache:
    """Thread-safe, asynchronous SQLite cache for Telegram media file_ids."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._busy_timeout_seconds = 5.0
        self._available = True
        self._reported_issues: set[str] = set()
        self._credential_scan_after_rowid = 0
        try:
            self._init_db()
        except (OSError, sqlite3.Error, _CacheSchemaError) as exc:
            self._report_db_issue(_sqlite_error_category(exc), disable=True)

    @property
    def available(self) -> bool:
        """Whether cache operations can currently use the backing database."""
        with self._state_lock:
            return self._available

    def _report_db_issue(self, category: str, *, disable: bool) -> None:
        with self._state_lock:
            if disable:
                self._available = False
            should_log = category not in self._reported_issues
            self._reported_issues.add(category)
        if should_log:
            if category == "busy" and not disable:
                logger.warning(
                    "Media cache temporarily skipped an operation after SQLite contention; "
                    "the cache remains enabled."
                )
            else:
                logger.warning(
                    "Media cache disabled after SQLite %s issue; sharing will continue "
                    "without cache. Stop the bot and follow the cache repair instructions "
                    "in docs/README.md.",
                    category,
                )

    def _handle_db_error(self, error: BaseException) -> _CacheUnavailable:
        category = _sqlite_error_category(error)
        self._report_db_issue(category, disable=category != "busy")
        return _CacheUnavailable(category)

    @contextlib.contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        busy_timeout_ms = int(self._busy_timeout_seconds * 1000)
        conn = sqlite3.connect(self.db_path, timeout=self._busy_timeout_seconds)
        try:
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        main_database_missing_or_empty = (
            not self.db_path.exists() or self.db_path.stat().st_size == 0
        )
        if main_database_missing_or_empty and any(
            Path(f"{self.db_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
        ):
            raise _CacheSchemaError("main database is missing while SQLite sidecars remain")
        conn = sqlite3.connect(self.db_path, timeout=self._busy_timeout_seconds)
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)};")
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_cache (
                        url TEXT PRIMARY KEY,
                        file_id TEXT NOT NULL,
                        media_kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        duration INTEGER,
                        video_height INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(media_cache)")}
                if "video_height" not in columns:
                    conn.execute("ALTER TABLE media_cache ADD COLUMN video_height INTEGER")
                    columns.add("video_height")
                missing_columns = _REQUIRED_CACHE_COLUMNS - columns
                if missing_columns:
                    raise _CacheSchemaError("media cache table is missing required columns")
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_media_cache_last_used
                    ON media_cache(last_used_at);
                    """
                )
        finally:
            conn.close()

    def _run_with_recovery(self, operation: Callable[[], _T]) -> _T:
        """Retry only a missing cache schema; never reset a live or corrupt database."""
        if not self.available:
            raise _CacheUnavailable("cache is disabled")
        try:
            return operation()
        except (OSError, sqlite3.Error) as exc:
            if _sqlite_error_category(exc) != "missing_schema":
                raise self._handle_db_error(exc) from exc

        try:
            with self._init_lock:
                if not self.available:
                    raise _CacheUnavailable("cache is disabled")
                self._init_db()
        except _CacheUnavailable:
            raise
        except (OSError, sqlite3.Error, _CacheSchemaError) as exc:
            raise self._handle_db_error(exc) from exc

        try:
            return operation()
        except (OSError, sqlite3.Error) as exc:
            raise self._handle_db_error(exc) from exc

    def _get_sync(self, norm_url: str) -> CachedMedia | None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT file_id, media_kind, title, duration, video_height
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
                SET last_used_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
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
                video_height=row[4],
            )

    def _set_sync(
        self,
        norm_url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
        video_height: int | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO media_cache (
                    url, file_id, media_kind, title, duration, video_height, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
                ON CONFLICT(url) DO UPDATE SET
                    file_id = excluded.file_id,
                    media_kind = excluded.media_kind,
                    title = excluded.title,
                    duration = excluded.duration,
                    video_height = excluded.video_height,
                    last_used_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
                """,
                (norm_url, file_id, kind.value, title, duration, video_height),
            )

    def _evict_sync(self, norm_url: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM media_cache WHERE url = ?", (norm_url,))
            return cursor.rowcount > 0

    def _evict_entry_sync(self, cached: CachedMedia) -> bool:
        """Delete only the exact file ID observed by the failed delivery."""
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM media_cache WHERE url = ? AND file_id = ?",
                (cached.url, cached.file_id),
            )
            return cursor.rowcount > 0

    def _get_with_recovery(self, norm_url: str) -> CachedMedia | None:
        return self._run_with_recovery(lambda: self._get_sync(norm_url))

    def _set_with_recovery(
        self,
        norm_url: str,
        file_id: str,
        kind: MediaKind,
        title: str,
        duration: int | None,
        video_height: int | None = None,
    ) -> None:
        self._run_with_recovery(
            lambda: self._set_sync(norm_url, file_id, kind, title, duration, video_height)
        )

    def _evict_with_recovery(self, norm_url: str) -> bool:
        return self._run_with_recovery(lambda: self._evict_sync(norm_url))

    def _evict_entry_with_recovery(self, cached: CachedMedia) -> bool:
        return self._run_with_recovery(lambda: self._evict_entry_sync(cached))

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
            return None
        except _CacheUnavailable:
            return None

    async def get_preferred_video(
        self,
        url: str,
        *,
        time_range: TimeRange | None = None,
        quality_policy: str = "auto-best",
    ) -> CachedMedia | None:
        """Reuse a prepared video when it meets the requested quality tier.

        A Best upload is preferable to Auto. For a named progressive tier,
        cross-policy reuse requires Telegram's measured output height.
        """
        if quality_policy == "source-best":
            return await self.get(url, time_range=time_range, quality_policy=quality_policy)
        if quality_policy == "auto-best":
            best = await self.get(url, time_range=time_range, quality_policy="source-best")
            auto = await self.get(url, time_range=time_range, quality_policy=quality_policy)
            if best is None or best.kind is not MediaKind.VIDEO:
                return auto
            if auto is None or auto.kind is not MediaKind.VIDEO:
                return best
            if best.video_height is None:
                return auto if auto.video_height is not None else best
            if auto.video_height is not None and auto.video_height > best.video_height:
                return auto
            return best

        if quality_policy == "balanced":
            balanced_candidates = [
                await self.get(url, time_range=time_range, quality_policy=policy)
                for policy in ("source-best", "auto-best", "balanced")
            ]
            known_quality = [
                candidate
                for candidate in balanced_candidates
                if candidate is not None
                and candidate.kind is MediaKind.VIDEO
                and candidate.video_height is not None
            ]
            if known_quality:
                return max(known_quality, key=lambda item: item.video_height or 0)
            # An exact-policy cache entry remains usable even when Telegram did
            # not report dimensions; cross-policy quality cannot be inferred.
            exact = balanced_candidates[2]
            return exact if exact is not None and exact.kind is MediaKind.VIDEO else None

        target_height = (
            int(quality_policy[:-1])
            if quality_policy.endswith("p") and quality_policy[:-1].isdigit()
            else None
        )
        if target_height is not None:
            qualified_candidates: list[CachedMedia] = []
            for policy in ("source-best", "auto-best", quality_policy):
                candidate = await self.get(url, time_range=time_range, quality_policy=policy)
                if (
                    candidate is not None
                    and candidate.kind is MediaKind.VIDEO
                    and candidate.video_height is not None
                    and candidate.video_height >= target_height
                ):
                    qualified_candidates.append(candidate)
            if qualified_candidates:
                return max(qualified_candidates, key=lambda item: item.video_height or 0)
        return await self.get(url, time_range=time_range, quality_policy=quality_policy)

    async def evict_entry(self, cached: CachedMedia) -> bool:
        """Remove the observed entry without erasing a concurrent replacement."""
        try:
            return await asyncio.to_thread(self._evict_entry_with_recovery, cached)
        except _CacheUnavailable:
            return False

    def _maintain_sync(self, max_entries: int, batch_size: int) -> tuple[int, int]:
        """Remove a bounded batch of credential keys and least-recently-used rows."""
        with self._maintenance_lock:
            next_scan_rowid = self._credential_scan_after_rowid

            def maintain() -> tuple[int, int, int]:
                nonlocal next_scan_rowid
                credential_rows_removed = 0
                rows_pruned = 0
                with self._connection() as conn:
                    candidates = conn.execute(
                        """
                        SELECT rowid, url FROM media_cache
                        WHERE rowid > ? AND instr(url, '@') > 0
                        ORDER BY rowid LIMIT ?
                        """,
                        (self._credential_scan_after_rowid, batch_size),
                    ).fetchall()
                    if not candidates and self._credential_scan_after_rowid:
                        candidates = conn.execute(
                            """
                            SELECT rowid, url FROM media_cache
                            WHERE instr(url, '@') > 0
                            ORDER BY rowid LIMIT ?
                            """,
                            (batch_size,),
                        ).fetchall()

                    if candidates:
                        next_scan_rowid = int(candidates[-1][0])
                        credential_keys = [
                            str(row[1])
                            for row in candidates
                            if _cache_key_has_userinfo(str(row[1]))
                        ]
                        if credential_keys:
                            placeholders = ",".join("?" for _ in credential_keys)
                            cursor = conn.execute(
                                f"DELETE FROM media_cache WHERE url IN ({placeholders})",
                                credential_keys,
                            )
                            credential_rows_removed = max(cursor.rowcount, 0)
                    else:
                        next_scan_rowid = 0

                    count_row = conn.execute(
                        "SELECT COUNT(*) FROM media_cache"
                    ).fetchone()
                    current_count = int(count_row[0]) if count_row is not None else 0
                    excess = max(0, current_count - max_entries)
                    prune_limit = min(excess, batch_size)
                    if prune_limit:
                        cursor = conn.execute(
                            """
                            DELETE FROM media_cache
                            WHERE rowid IN (
                                SELECT rowid FROM media_cache
                                ORDER BY last_used_at ASC, rowid ASC
                                LIMIT ?
                            )
                            """,
                            (prune_limit,),
                        )
                        rows_pruned = max(cursor.rowcount, 0)

                return credential_rows_removed, rows_pruned, next_scan_rowid

            try:
                credential_rows_removed, rows_pruned, committed_scan_rowid = (
                    self._run_with_recovery(maintain)
                )
            except _CacheUnavailable:
                return 0, 0
            self._credential_scan_after_rowid = committed_scan_rowid
            return rows_pruned, credential_rows_removed

    async def maintain(
        self,
        *,
        max_entries: int = _CACHE_MAX_ENTRIES,
        batch_size: int = _CACHE_MAINTENANCE_BATCH_SIZE,
    ) -> tuple[int, int]:
        """Prune old metadata and historical credential keys in bounded batches.

        Returns (least-recently-used rows removed, credential-bearing rows removed).
        """
        bounded_max = max(0, max_entries)
        bounded_batch = max(1, min(batch_size, _CACHE_MAINTENANCE_BATCH_SIZE))
        return await asyncio.to_thread(self._maintain_sync, bounded_max, bounded_batch)

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
        video_height: int | None = None,
    ) -> None:
        """Cache media file_id under the normalized URL.

        If the DB file was deleted mid-run, recreates it and retries once.
        Database failures are isolated so delivery can continue without the cache.
        """
        if not is_public_cacheable_url(url):
            logger.debug("Skipping cache for non-public URL: %s", safe_url_for_log(url))
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
                self._set_with_recovery, key, file_id, kind, title, duration, video_height
            )
            logger.debug("Cached file_id for %s (%s)", key, kind.value)
        except _CacheUnavailable:
            return

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
        except _CacheUnavailable:
            return
        if removed:
            logger.info("Evicted %s from media cache", safe_url_for_log(key))
