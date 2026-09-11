"""Unit tests for SQLite MediaCache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_share_bot.cache import MediaCache
from telegram_share_bot.downloader import MediaKind


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


if __name__ == "__main__":
    unittest.main()
