"""Cross-layer coverage for the quiet-audio processing policy."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_share_bot.config import Settings, load_settings
from telegram_share_bot.handlers.direct import _run_direct_download
from telegram_share_bot.handlers.prepare import _prepare_inline_media
from telegram_share_bot.media.jobs import download_media
from telegram_share_bot.media.models import DownloadedMedia, DownloadError, MediaFormat, MediaKind
from telegram_share_bot.media.work import MediaWorkLease
from telegram_share_bot.storage.media_cache import MediaCache


class TestAudioPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_cache_modes_remain_separate_including_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "cache.db"
            original = MediaCache(db)
            normalized = MediaCache(db, normalize_audio=True)
            for form, kind in (
                (MediaFormat.VIDEO, MediaKind.VIDEO),
                (MediaFormat.AUDIO, MediaKind.AUDIO),
            ):
                options = dict(media_format=form, quality_policy="source-best")
                url = "https://youtu.be/quiet-sample"
                await original.set(
                    url,
                    "original",
                    kind,
                    "title",
                    20,
                    video_width=720,
                    video_height=1280,
                    **options,
                )
                self.assertIsNone(await normalized.get(url, **options))
                await normalized.set(
                    url,
                    "normalized",
                    kind,
                    "title",
                    20,
                    video_width=720,
                    video_height=1280,
                    **options,
                )
                self.assertEqual((await normalized.get(url, **options)).file_id, "normalized")
                await normalized.evict(url, **options)
                self.assertIsNone(await normalized.get(url, **options))
                self.assertEqual((await original.get(url, **options)).file_id, "original")

    async def test_normalizer_uses_original_deadline_in_tracked_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"source")
            media = DownloadedMedia(source, "title", MediaKind.VIDEO, 20, 720, 1280)
            lease = MediaWorkLease(None, time.monotonic() + 10)
            with (
                patch("telegram_share_bot.media.jobs._download_sync", return_value=media),
                patch(
                    "telegram_share_bot.media.jobs._normalize_quiet_audio", return_value=media
                ) as normalize,
            ):
                result = await download_media(
                    "https://youtu.be/example",
                    Path(temporary),
                    1000,
                    10,
                    normalize_quiet_audio=True,
                    work_lease=lease,
                )
            self.assertEqual(result, media)
            self.assertEqual(normalize.call_args.kwargs["deadline"], lease.deadline)
            await lease.release()

    async def test_failed_normalization_cleans_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            source = work / "source.mp4"
            source.write_bytes(b"source")
            media = DownloadedMedia(source, "title", MediaKind.VIDEO, 20)
            with (
                patch("telegram_share_bot.media.jobs._download_sync", return_value=media),
                patch(
                    "telegram_share_bot.media.jobs._normalize_quiet_audio",
                    side_effect=DownloadError("normalization failed", retryable=True),
                ),
            ):
                with self.assertRaisesRegex(DownloadError, "normalization failed"):
                    await download_media(
                        "https://youtu.be/example",
                        Path(temporary),
                        1000,
                        10,
                        normalize_quiet_audio=True,
                    )
            self.assertFalse(work.exists())

    async def test_inline_normalization_bypasses_direct_import_and_offers_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = MagicMock()
            context.application.bot_data = {
                "settings": Settings(
                    "test:token", 42, 1000, 10, root, root / "cache.db", False, allow_public=True
                ),
                "media_cache": MediaCache(root / "cache.db", normalize_audio=True),
            }
            context.bot.edit_message_text = AsyncMock()
            with (
                patch(
                    "telegram_share_bot.handlers.prepare.get_direct_stream", new_callable=AsyncMock
                ) as direct,
                patch(
                    "telegram_share_bot.handlers.prepare.download_media",
                    new_callable=AsyncMock,
                    side_effect=DownloadError(
                        "Volume failed. Please try again later.", retryable=True
                    ),
                ) as download,
            ):
                await _prepare_inline_media(
                    context,
                    inline_message_id="quiet-audio",
                    user_id=42,
                    url="https://youtu.be/quiet",
                    result_id="quiet-result",
                    media_format=MediaFormat.AUDIO,
                )
            direct.assert_not_awaited()
            self.assertTrue(download.await_args.kwargs["normalize_quiet_audio"])
            markup = context.bot.edit_message_text.await_args.kwargs["reply_markup"]
            self.assertEqual(markup.inline_keyboard[0][0].callback_data, "retry:quiet-result")

    async def test_private_normalization_bypasses_direct_audio_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = MagicMock()
            context.application.bot_data = {
                "settings": Settings(
                    "test:token", 42, 1000, 10, root, root / "cache.db", False, allow_public=True
                ),
                "media_cache": MediaCache(root / "cache.db", normalize_audio=True),
            }
            status = MagicMock()
            status.edit_text = AsyncMock()
            with (
                patch(
                    "telegram_share_bot.handlers.direct.get_direct_stream", new_callable=AsyncMock
                ) as direct,
                patch(
                    "telegram_share_bot.handlers.direct.download_media",
                    new_callable=AsyncMock,
                    side_effect=DownloadError(
                        "Volume failed. Please try again later.", retryable=True
                    ),
                ) as download,
            ):
                await _run_direct_download(
                    context,
                    chat_id=42,
                    user_id=42,
                    url="https://youtu.be/quiet",
                    custom_caption=None,
                    time_range=None,
                    status_message=status,
                    media_format=MediaFormat.AUDIO,
                )
            direct.assert_not_awaited()
            self.assertTrue(download.await_args.kwargs["normalize_quiet_audio"])
            self.assertIn("Please try again later", status.edit_text.await_args.args[0])


class TestAudioConfiguration(unittest.TestCase):
    def test_enabled_by_default_and_can_be_disabled(self) -> None:
        environment = {"BOT_TOKEN": "valid:token", "STORAGE_CHAT_ID": "42", "ALLOW_PUBLIC": "true"}
        with patch.dict("os.environ", environment, clear=True):
            self.assertTrue(load_settings().normalize_quiet_audio)
        with patch.dict(
            "os.environ", {**environment, "NORMALIZE_QUIET_AUDIO": "false"}, clear=True
        ):
            self.assertFalse(load_settings().normalize_quiet_audio)
        with patch.dict(
            "os.environ", {**environment, "NORMALIZE_QUIET_AUDIO": "invalid"}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                load_settings()
