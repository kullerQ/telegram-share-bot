"""Structural validation for yt-dlp's single-media metadata results."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_share_bot import strings
from telegram_share_bot.media.metadata import _MAX_WRAPPED_INFO_ENTRIES, _pick_info
from telegram_share_bot.media.models import DownloadError
from telegram_share_bot.media.transfer import _download_sync


class TestPickInfo(unittest.TestCase):
    def test_accepts_a_normal_media_result(self) -> None:
        info = {"id": "video-id", "title": "A video"}
        self.assertIs(_pick_info(info), info)

    def test_accepts_one_usable_item_in_a_wrapper(self) -> None:
        item = {"id": "video-id", "title": "A video", "formats": [{"format_id": "1"}]}
        result = _pick_info({"_type": "playlist", "entries": [None, {}, item]})
        self.assertIs(result, item)

    def test_preserves_watch_url_with_playlist_query_for_single_item(self) -> None:
        watch_url = "https://www.youtube.com/watch?v=video-id&list=playlist-id"
        item = {"id": "video-id", "url": watch_url, "title": "A video"}
        self.assertEqual(_pick_info({"entries": [item]})["url"], watch_url)

    def test_rejects_a_second_usable_item(self) -> None:
        with self.assertRaisesRegex(DownloadError, strings.DOWNLOAD_PLAYLIST_UNSUPPORTED):
            _pick_info({"entries": [{"id": "one"}, {"id": "two"}]})

    def test_rejects_empty_or_malformed_wrappers(self) -> None:
        invalid_results = (
            {"entries": None},
            {"entries": []},
            {"entries": [None]},
            {"entries": [{}]},
            {"entries": ["not-media"]},
            {"entries": {}},
            {"entries": 42},
            {"_type": "playlist"},
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaisesRegex(DownloadError, strings.DOWNLOAD_PLAYLIST_UNSUPPORTED):
                    _pick_info(result)

    def test_over_budget_generator_is_stopped_after_one_lookahead(self) -> None:
        consumed: list[int] = []

        def entries():
            for index in range(_MAX_WRAPPED_INFO_ENTRIES + 1000):
                consumed.append(index)
                yield None

        with self.assertRaisesRegex(DownloadError, strings.DOWNLOAD_PLAYLIST_UNSUPPORTED):
            _pick_info({"entries": entries()})
        self.assertEqual(len(consumed), _MAX_WRAPPED_INFO_ENTRIES + 1)

    def test_generator_failure_is_reported_as_unsupported_result(self) -> None:
        def broken_entries():
            yield None
            raise RuntimeError("extractor wrapper failed")

        with self.assertRaisesRegex(DownloadError, strings.DOWNLOAD_PLAYLIST_UNSUPPORTED):
            _pick_info({"entries": broken_entries()})


class TestResultValidationBeforeTransfer(unittest.TestCase):
    def test_multiple_entries_are_rejected_before_yt_dlp_transfer(self) -> None:
        wrapped_info = {
            "_type": "playlist",
            "entries": [
                {"id": "first-video", "url": "https://cdn.example/first.mp4"},
                {"id": "second-video", "url": "https://cdn.example/second.mp4"},
            ],
        }
        ydl = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with (
                patch(
                    "telegram_share_bot.media.transfer.try_tiktok_slideshow",
                    return_value=None,
                ),
                patch(
                    "telegram_share_bot.media.transfer._safe_dns_resolution",
                    return_value=contextlib.nullcontext(),
                ),
                patch(
                    "telegram_share_bot.media.transfer._extract_info_cached",
                    return_value=(wrapped_info, False),
                ),
                patch("telegram_share_bot.media.transfer.create_youtube_dl") as factory,
            ):
                factory.return_value.__enter__.return_value = ydl
                with self.assertRaisesRegex(
                    DownloadError, strings.DOWNLOAD_PLAYLIST_UNSUPPORTED
                ):
                    _download_sync(
                        "https://www.youtube.com/watch?v=first-video&list=playlist-id",
                        root,
                        max_file_bytes=45 * 1024 * 1024,
                        timeout_seconds=30,
                    )

            ydl.process_ie_result.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])
