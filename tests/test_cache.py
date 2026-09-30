"""Unit tests for SQLite MediaCache."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot.media.models import MediaFormat, MediaKind
from telegram_share_bot.storage.media_cache import MediaCache


class TestMediaCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cache.db"
        self.cache = MediaCache(self.db_path)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_set_and_get(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        file_id = "BAACAgIAAxkBAAI..."
        title = "Never Gonna Give You Up"
        duration = 212

        await self.cache.set(
            url=url,
            file_id=file_id,
            kind=MediaKind.VIDEO,
            title=title,
            duration=duration,
        )

        cached = await self.cache.get(url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, file_id)
        self.assertEqual(cached.kind, MediaKind.VIDEO)
        self.assertEqual(cached.title, title)
        self.assertEqual(cached.duration, duration)

    async def test_locked_database_skips_cache_without_removing_wal(self) -> None:
        url = "https://example.com/locked-cache"
        await self.cache.set(url, "file-before-lock", MediaKind.VIDEO, "Title", 10)

        reader = sqlite3.connect(self.db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM media_cache").fetchone()
        await self.cache.set(url, "file-after-reader", MediaKind.VIDEO, "Title", 10)

        wal_path = Path(f"{self.db_path}-wal")
        self.assertTrue(wal_path.exists())
        wal_before = wal_path.read_bytes()
        writer = sqlite3.connect(self.db_path, timeout=0.1)
        writer.execute("PRAGMA busy_timeout = 100")
        writer.execute("BEGIN IMMEDIATE")
        self.cache._busy_timeout_seconds = 0.02

        try:
            with self.assertLogs("telegram_share_bot.storage.media_cache", level="WARNING"):
                self.assertIsNone(await self.cache.get(url))

            self.assertTrue(self.cache.available)
            self.assertEqual(wal_path.read_bytes(), wal_before)
        finally:
            writer.rollback()
            writer.close()
            reader.rollback()
            reader.close()
        cached = await self.cache.get(url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "file-after-reader")

    async def test_readonly_error_disables_cache_without_recreating_database(self) -> None:
        url = "https://example.com/readonly-cache"
        await self.cache.set(url, "file-id", MediaKind.VIDEO, "Title", 10)
        database_before = self.db_path.read_bytes()
        with patch.object(
            self.cache, "_connection", side_effect=sqlite3.OperationalError("readonly database")
        ):
            with self.assertLogs("telegram_share_bot.storage.media_cache", level="WARNING"):
                self.assertIsNone(await self.cache.get(url))
        self.assertFalse(self.cache.available)
        self.assertEqual(self.db_path.read_bytes(), database_before)
        await self.cache.set(url, "replacement", MediaKind.VIDEO, "Title", 10)
        self.assertEqual(self.db_path.read_bytes(), database_before)

    async def test_corrupt_database_and_sidecars_are_left_untouched(self) -> None:
        self.temp_dir.cleanup()
        corrupt_path = Path(self.temp_dir.name) / "corrupt.db"
        corrupt_path.parent.mkdir(parents=True)
        corrupt_path.write_bytes(b"not a sqlite database")
        database_before = corrupt_path.read_bytes()

        with self.assertLogs("telegram_share_bot.storage.media_cache", level="WARNING"):
            corrupt_cache = MediaCache(corrupt_path)

        self.assertFalse(corrupt_cache.available)
        self.assertIsNone(await corrupt_cache.get("https://example.com/video"))
        self.assertEqual(corrupt_path.read_bytes(), database_before)

    async def test_missing_main_database_with_sidecars_is_not_recreated(self) -> None:
        self.temp_dir.cleanup()
        missing_path = Path(self.temp_dir.name) / "missing.db"
        missing_path.parent.mkdir(parents=True)
        sidecar_data = {"-wal": b"keep wal", "-shm": b"keep shm", "-journal": b"keep journal"}
        for suffix, content in sidecar_data.items():
            Path(f"{missing_path}{suffix}").write_bytes(content)

        with self.assertLogs("telegram_share_bot.storage.media_cache", level="WARNING"):
            cache = MediaCache(missing_path)

        self.assertFalse(cache.available)
        self.assertFalse(missing_path.exists())
        for suffix, content in sidecar_data.items():
            self.assertEqual(Path(f"{missing_path}{suffix}").read_bytes(), content)

    async def test_missing_table_is_initialized_without_sidecar_deletion(self) -> None:
        url = "https://example.com/missing-cache-schema"
        reader = sqlite3.connect(self.db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM media_cache").fetchone()
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DROP TABLE media_cache")
        wal_path = Path(f"{self.db_path}-wal")
        self.assertTrue(wal_path.exists())

        try:
            # A live main database may safely initialize the missing disposable cache table.
            self.assertIsNone(await self.cache.get(url))
            self.assertTrue(self.cache.available)
            self.assertTrue(wal_path.exists())
            await self.cache.set(url, "recovered", MediaKind.VIDEO, "Title", 10)
            self.assertIsNotNone(await self.cache.get(url))
        finally:
            reader.close()

    async def test_maintenance_prunes_oldest_rows_in_bounded_batches(self) -> None:
        urls = [f"https://example.com/retention/{index}" for index in range(12)]
        for index, url in enumerate(urls):
            await self.cache.set(url, f"file-{index}", MediaKind.VIDEO, "Title", 10)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE media_cache SET last_used_at = '2000-01-01 00:00:00' "
                "|| substr('00' || rowid, -2)"
            )

        removed, credentials = await self.cache.maintain(max_entries=8, batch_size=3)
        self.assertEqual((removed, credentials), (3, 0))
        self.assertEqual(await self._cache_row_count(), 9)
        removed, credentials = await self.cache.maintain(max_entries=8, batch_size=3)
        self.assertEqual((removed, credentials), (1, 0))
        self.assertEqual(await self._cache_row_count(), 8)
        oldest = await self.cache.get(urls[0])
        newest = await self.cache.get(urls[-1])
        self.assertIsNone(oldest)
        self.assertIsNotNone(newest)

    async def test_maintenance_removes_legacy_userinfo_keys_without_logging_them(self) -> None:
        credential_url = (
            "https://account-name:secret-value@example.com/video"
            "#format=video&quality=auto-best&delivery=2"
        )
        path_at_url = "https://example.com/user@mention#format=video&quality=auto-best"
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            for key, file_id in ((credential_url, "credential-file"), (path_at_url, "safe-file")):
                conn.execute(
                    "INSERT INTO media_cache (url, file_id, media_kind, title, duration) "
                    "VALUES (?, ?, 'video', 'Title', 10)",
                    (key, file_id),
                )

        removed, credentials = await self.cache.maintain(max_entries=10, batch_size=10)
        self.assertEqual((removed, credentials), (0, 1))
        self.assertIsNone(self.cache._get_sync(credential_url))
        retained = self.cache._get_sync(path_at_url)
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(retained.file_id, "safe-file")

    async def test_concurrent_cache_replacement_survives_retention(self) -> None:
        urls = [f"https://example.com/concurrent-retention/{index}" for index in range(6)]
        for index, url in enumerate(urls):
            await self.cache.set(url, f"file-{index}", MediaKind.VIDEO, "Title", 10)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE media_cache SET last_used_at = '2000-01-01 00:00:00'")

        await asyncio.gather(
            self.cache.set(urls[0], "replacement-file", MediaKind.VIDEO, "Title", 10),
            self.cache.maintain(max_entries=4, batch_size=2),
        )

        cached = await self.cache.get(urls[0])
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "replacement-file")
        self.assertEqual(await self._cache_row_count(), 4)

    async def test_sqlite_backup_api_copies_a_consistent_cache_snapshot(self) -> None:
        await self.cache.set(
            "https://example.com/backup", "backup-file", MediaKind.VIDEO, "Title", 10
        )
        backup_path = Path(self.temp_dir.name) / "backup.db"
        with (
            contextlib.closing(sqlite3.connect(self.db_path)) as source,
            contextlib.closing(sqlite3.connect(backup_path)) as destination,
        ):
            source.backup(destination)
        with contextlib.closing(sqlite3.connect(backup_path)) as backup:
            row = backup.execute(
                "SELECT file_id FROM media_cache WHERE file_id = ?", ("backup-file",)
            ).fetchone()
        self.assertEqual(row, ("backup-file",))

    async def _cache_row_count(self) -> int:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM media_cache").fetchone()
        assert row is not None
        return int(row[0])

    async def test_normalization_lookup(self) -> None:
        # Saved with youtu.be shortlink
        saved_url = "https://youtu.be/dQw4w9WgXcQ?si=tracking123"
        await self.cache.set(
            url=saved_url,
            file_id="cached_file_id_123",
            kind=MediaKind.VIDEO,
            title="Video Title",
            duration=120,
        )

        # Looked up with standard youtube.com link with different tracking params
        lookup_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share"
        cached = await self.cache.get(lookup_url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "cached_file_id_123")

    async def test_credential_bearing_urls_are_never_cached(self) -> None:
        url = "https://alice:secret@youtube.com/watch?v=dQw4w9WgXcQ"
        await self.cache.set(
            url,
            "private-file-id",
            MediaKind.VIDEO,
            "Private",
            30,
        )

        self.assertIsNone(await self.cache.get(url))

    async def test_evict(self) -> None:
        url = "https://x.com/user/status/12345"
        await self.cache.set(
            url=url,
            file_id="tweet_video_file_id",
            kind=MediaKind.VIDEO,
            title="Tweet",
            duration=15,
        )

        self.assertIsNotNone(await self.cache.get(url))
        await self.cache.evict(url)
        self.assertIsNone(await self.cache.get(url))

    async def test_evict_entry_preserves_a_concurrent_replacement(self) -> None:
        url = "https://x.com/user/status/replaced-cache-entry"
        await self.cache.set(
            url=url,
            file_id="observed-old-file-id",
            kind=MediaKind.VIDEO,
            title="Old",
            duration=15,
            quality_policy="auto-best",
        )
        observed = await self.cache.get(url, quality_policy="auto-best")
        self.assertIsNotNone(observed)
        assert observed is not None

        await self.cache.set(
            url=url,
            file_id="concurrent-new-file-id",
            kind=MediaKind.VIDEO,
            title="New",
            duration=15,
            quality_policy="auto-best",
        )

        self.assertFalse(await self.cache.evict_entry(observed))
        replacement = await self.cache.get(url, quality_policy="auto-best")
        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertEqual(replacement.file_id, "concurrent-new-file-id")

    async def test_upsert(self) -> None:
        url = "https://example.com/audio.mp3"
        await self.cache.set(
            url=url,
            file_id="old_file_id",
            kind=MediaKind.AUDIO,
            title="Song V1",
            duration=180,
        )

        # Update with new file_id
        await self.cache.set(
            url=url,
            file_id="new_file_id",
            kind=MediaKind.AUDIO,
            title="Song V2",
            duration=185,
        )

        cached = await self.cache.get(url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "new_file_id")
        self.assertEqual(cached.title, "Song V2")
        self.assertEqual(cached.duration, 185)

    async def test_format_and_quality_variants_are_isolated(self) -> None:
        url = "https://www.youtube.com/watch?v=variant12345"
        await self.cache.set(
            url,
            "video-id",
            MediaKind.VIDEO,
            "Video",
            30,
            media_format=MediaFormat.VIDEO,
        )
        await self.cache.set(
            url,
            "audio-id",
            MediaKind.AUDIO,
            "Audio",
            30,
            media_format=MediaFormat.AUDIO,
        )

        self.assertEqual(
            (await self.cache.get(url, media_format=MediaFormat.VIDEO)).file_id,
            "video-id",
        )
        self.assertEqual(
            (await self.cache.get(url, media_format=MediaFormat.AUDIO)).file_id,
            "audio-id",
        )
        self.assertIsNone(
            await self.cache.get(url, media_format=MediaFormat.VIDEO, quality_policy="720p")
        )
        for policy, file_id in (
            ("auto-best", "auto-id"),
            ("source-best", "best-id"),
            ("balanced", "balanced-id"),
        ):
            await self.cache.set(
                url,
                file_id,
                MediaKind.VIDEO,
                "Video",
                30,
                quality_policy=policy,
            )
        for policy, file_id in (
            ("auto-best", "auto-id"),
            ("source-best", "best-id"),
            ("balanced", "balanced-id"),
        ):
            cached = await self.cache.get(url, quality_policy=policy)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.file_id, file_id)

    async def test_unverified_legacy_video_is_not_reused(self) -> None:
        from telegram_share_bot.storage.media_cache import _legacy_cache_key

        url = "https://www.youtube.com/watch?v=legacy12345"
        legacy_key = _legacy_cache_key(url)
        assert legacy_key is not None
        await asyncio.to_thread(
            self.cache._set_sync,
            legacy_key,
            "legacy-video-id",
            MediaKind.VIDEO,
            "Legacy",
            45,
        )

        video = await self.cache.get(url, media_format=MediaFormat.VIDEO)
        self.assertIsNone(video)
        self.assertIsNone(await self.cache.get(url, media_format=MediaFormat.AUDIO))
        self.assertIsNone(
            await self.cache.get(url, media_format=MediaFormat.VIDEO, quality_policy="720p")
        )
        self.assertIsNone(await self.cache.get(url, quality_policy="auto-best"))

    async def test_best_video_preferred_over_auto_for_same_clip(self) -> None:
        from telegram_share_bot.media.models import TimeRange

        url = "https://www.youtube.com/watch?v=quality12345"
        clip = TimeRange(start=60, end=120)
        await self.cache.set(
            url,
            "auto",
            MediaKind.VIDEO,
            "Auto",
            60,
            time_range=clip,
            quality_policy="auto-best",
            video_height=720,
        )
        await self.cache.set(
            url,
            "best",
            MediaKind.VIDEO,
            "Best",
            60,
            time_range=clip,
            quality_policy="source-best",
            video_height=1080,
        )
        chosen = await self.cache.get_preferred_video(url, time_range=clip)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.file_id, "best")
        await self.cache.set(
            url,
            "auto-higher",
            MediaKind.VIDEO,
            "Auto",
            60,
            time_range=clip,
            quality_policy="auto-best",
            video_height=1440,
        )
        higher = await self.cache.get_preferred_video(url, time_range=clip)
        self.assertIsNotNone(higher)
        assert higher is not None
        self.assertEqual(higher.file_id, "auto-higher")
        await self.cache.evict_entry(higher)
        self.assertIsNone(await self.cache.get_preferred_video(url))
        await self.cache.evict_entry(chosen)
        fallback = await self.cache.get_preferred_video(url, time_range=clip)
        self.assertIsNone(fallback)

    async def test_progressive_tier_reuses_only_measured_sufficient_video(self) -> None:
        url = "https://www.youtube.com/watch?v=tier1234567"
        await self.cache.set(
            url,
            "best-low",
            MediaKind.VIDEO,
            "Best",
            60,
            quality_policy="source-best",
            video_height=480,
        )
        await self.cache.set(
            url,
            "balanced",
            MediaKind.VIDEO,
            "Balanced",
            60,
            quality_policy="balanced",
            video_height=720,
        )
        chosen = await self.cache.get_preferred_video(url, quality_policy="balanced")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.file_id, "balanced")
        self.assertIsNone(await self.cache.get_preferred_video(url, quality_policy="1080p"))

    async def test_balanced_prefers_higher_known_cached_video(self) -> None:
        url = "https://www.youtube.com/watch?v=balanced-cache"
        await self.cache.set(
            url,
            "balanced-id",
            MediaKind.VIDEO,
            "Balanced",
            30,
            quality_policy="balanced",
            video_height=720,
        )
        await self.cache.set(
            url,
            "best-id",
            MediaKind.VIDEO,
            "Best",
            30,
            quality_policy="source-best",
            video_height=1080,
        )
        cached = await self.cache.get_preferred_video(url, quality_policy="balanced")
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "best-id")

    async def test_balanced_does_not_infer_quality_from_other_policy(self) -> None:
        url = "https://www.youtube.com/watch?v=unknown-cache-quality"
        await self.cache.set(
            url,
            "best-without-height",
            MediaKind.VIDEO,
            "Best",
            30,
            quality_policy="source-best",
        )
        self.assertIsNone(await self.cache.get_preferred_video(url, quality_policy="balanced"))
        await self.cache.set(
            url,
            "best-high",
            MediaKind.VIDEO,
            "Best",
            60,
            quality_policy="source-best",
            video_height=1440,
        )
        chosen = await self.cache.get_preferred_video(url, quality_policy="1080p")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.file_id, "best-high")

    async def test_existing_database_adds_video_height_without_losing_entries(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DROP TABLE media_cache")
            conn.execute(
                """CREATE TABLE media_cache (
                    url TEXT PRIMARY KEY, file_id TEXT NOT NULL,
                    media_kind TEXT NOT NULL, title TEXT NOT NULL, duration INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """INSERT INTO media_cache (url, file_id, media_kind, title, duration)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    "https://example.com/video#format=video&quality=auto-best",
                    "old",
                    "video",
                    "Old",
                    30,
                ),
            )
        self.cache._init_db()
        cached = await self.cache.get("https://example.com/video", quality_policy="auto-best")
        self.assertIsNone(cached)
        cached = self.cache._get_sync("https://example.com/video#format=video&quality=auto-best")
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "old")
        self.assertIsNone(cached.video_height)

    async def test_get_recovers_when_db_file_deleted(self) -> None:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await self.cache.set(
            url=url,
            file_id="file_before_delete",
            kind=MediaKind.VIDEO,
            title="Before",
            duration=10,
        )
        self.assertTrue(self.db_path.exists())
        self.db_path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

        # Cache miss (recreated empty DB), no exception
        self.assertIsNone(await self.cache.get(url))
        self.assertTrue(self.db_path.exists())

        # Writes work again after recovery
        await self.cache.set(
            url=url,
            file_id="file_after_recreate",
            kind=MediaKind.VIDEO,
            title="After",
            duration=11,
        )
        cached = await self.cache.get(url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "file_after_recreate")

    async def test_set_recovers_when_db_file_deleted(self) -> None:
        url = "https://www.youtube.com/watch?v=abc12345678"
        self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

        await self.cache.set(
            url=url,
            file_id="recovered_id",
            kind=MediaKind.VIDEO,
            title="Recovered",
            duration=5,
        )
        cached = await self.cache.get(url)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.file_id, "recovered_id")

    async def test_evict_when_db_file_deleted(self) -> None:
        url = "https://x.com/user/status/999"
        self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

        # Must not raise
        await self.cache.evict(url)


if __name__ == "__main__":
    unittest.main()
