"""Unit tests for caption mode parsing, extraction, and resolution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot.config import (
    DEFAULT_CAPTION_MODE,
    TELEGRAM_CAPTION_MAX_LENGTH,
    CaptionMode,
    load_settings,
    parse_caption_mode,
)
from telegram_share_bot.downloader import (
    extract_url_and_caption,
    resolve_caption,
    sanitize_caption,
)


class TestCaptionExtraction(unittest.TestCase):
    def test_url_only(self) -> None:
        url, caption = extract_url_and_caption("https://youtube.com/watch?v=abc")
        self.assertEqual(url, "https://youtube.com/watch?v=abc")
        self.assertIsNone(caption)

    def test_url_with_custom_caption(self) -> None:
        url, caption = extract_url_and_caption(
            "https://youtube.com/watch?v=abc my cool caption"
        )
        self.assertEqual(url, "https://youtube.com/watch?v=abc")
        self.assertEqual(caption, "my cool caption")

    def test_url_with_trailing_punctuation_then_caption(self) -> None:
        url, caption = extract_url_and_caption(
            "https://youtube.com/watch?v=abc. hello world"
        )
        self.assertEqual(url, "https://youtube.com/watch?v=abc")
        self.assertEqual(caption, "hello world")

    def test_no_url(self) -> None:
        url, caption = extract_url_and_caption("just some text")
        self.assertIsNone(url)
        self.assertIsNone(caption)


class TestCaptionSanitizeAndResolve(unittest.TestCase):
    def test_sanitize_strips_controls_and_nulls(self) -> None:
        self.assertEqual(
            sanitize_caption("hi\x00there\x07now"),
            "hitherenow",
        )
        self.assertEqual(sanitize_caption("line1\nline2"), "line1\nline2")

    def test_sanitize_truncates(self) -> None:
        long = "a" * (TELEGRAM_CAPTION_MAX_LENGTH + 50)
        result = sanitize_caption(long)
        assert result is not None
        self.assertEqual(len(result), TELEGRAM_CAPTION_MAX_LENGTH)

    def test_resolve_off(self) -> None:
        self.assertIsNone(
            resolve_caption(
                CaptionMode.OFF,
                media_title="Title",
                custom_caption="Custom",
            )
        )

    def test_resolve_media(self) -> None:
        self.assertEqual(
            resolve_caption(
                CaptionMode.MEDIA,
                media_title="Video Title",
                custom_caption="ignored",
            ),
            "Video Title",
        )

    def test_resolve_custom_uses_user_text_only(self) -> None:
        self.assertEqual(
            resolve_caption(
                CaptionMode.CUSTOM,
                media_title="Video Title",
                custom_caption="My caption",
            ),
            "My caption",
        )
        self.assertIsNone(
            resolve_caption(
                CaptionMode.CUSTOM,
                media_title="Video Title",
                custom_caption=None,
            )
        )


class TestCaptionModeConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp_dir.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_caption_mode_defaults(self) -> None:
        self.assertEqual(parse_caption_mode("CAPTION_MODE", None), DEFAULT_CAPTION_MODE)
        self.assertEqual(parse_caption_mode("CAPTION_MODE", "  "), DEFAULT_CAPTION_MODE)

    def test_parse_caption_mode_valid(self) -> None:
        self.assertEqual(parse_caption_mode("CAPTION_MODE", "media"), CaptionMode.MEDIA)
        self.assertEqual(
            parse_caption_mode("CAPTION_MODE", "CUSTOM"), CaptionMode.CUSTOM
        )
        self.assertEqual(parse_caption_mode("CAPTION_MODE", "off"), CaptionMode.OFF)

    def test_parse_caption_mode_invalid_non_interactive(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                parse_caption_mode("CAPTION_MODE", "both")

    def test_load_settings_caption_mode(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOW_PUBLIC": "true",
            "CAPTION_MODE": "custom",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings(env_file=self.env_file)
            self.assertEqual(settings.caption_mode, CaptionMode.CUSTOM)


if __name__ == "__main__":
    unittest.main()
