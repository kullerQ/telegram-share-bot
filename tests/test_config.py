"""Unit tests for strict configuration parsing, validation, and reset fallback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_share_bot.config import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    load_settings,
    parse_bool,
    parse_int,
    parse_path,
)


class TestConfigValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp_dir.name) / ".env"
        self.env_file.write_text("DELETE_STORAGE_MESSAGES=wrong\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # --- parse_bool tests ---

    def test_parse_bool_defaults_when_empty_or_none(self) -> None:
        self.assertFalse(parse_bool("PARAM", None, default=False))
        self.assertTrue(parse_bool("PARAM", "", default=True))
        self.assertFalse(parse_bool("PARAM", "   ", default=False))

    def test_parse_bool_valid_truthy(self) -> None:
        for val in ("true", "TRUE", "1", "yes", "YES", "y", "on"):
            with self.subTest(val=val):
                self.assertTrue(parse_bool("PARAM", val, default=False))

    def test_parse_bool_valid_falsy(self) -> None:
        for val in ("false", "FALSE", "0", "no", "NO", "n", "off"):
            with self.subTest(val=val):
                self.assertFalse(parse_bool("PARAM", val, default=True))

    def test_parse_bool_invalid_non_interactive_raises(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                parse_bool("DELETE_STORAGE_MESSAGES", "maybe", default=False)
            self.assertIn("Invalid configuration for DELETE_STORAGE_MESSAGES", str(ctx.exception))

    def test_parse_bool_invalid_interactive_accept_reset(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            res = parse_bool(
                "DELETE_STORAGE_MESSAGES",
                "invalid_value",
                default=False,
                env_file=self.env_file,
            )
            self.assertFalse(res)
            # Verify .env file was updated
            content = self.env_file.read_text(encoding="utf-8")
            self.assertIn("DELETE_STORAGE_MESSAGES=false", content)

    def test_parse_bool_invalid_interactive_decline_reset(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                parse_bool(
                    "DELETE_STORAGE_MESSAGES",
                    "invalid_value",
                    default=False,
                    env_file=self.env_file,
                )
            self.assertIn("Please fix it in your .env file", str(ctx.exception))

    # --- parse_int tests ---

    def test_parse_int_defaults_when_empty_or_none(self) -> None:
        self.assertEqual(parse_int("PARAM", None, default=42), 42)
        self.assertEqual(parse_int("PARAM", "", default=42), 42)

    def test_download_timeout_default_is_120_seconds(self) -> None:
        self.assertEqual(DEFAULT_DOWNLOAD_TIMEOUT_SECONDS, 120)

    def test_parse_int_valid_within_bounds(self) -> None:
        self.assertEqual(
            parse_int("MAX_FILE_BYTES", "1048576", default=DEFAULT_MAX_FILE_BYTES, min_value=1024),
            1048576,
        )

    def test_parse_int_invalid_string_non_interactive(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                parse_int("MAX_FILE_BYTES", "not_a_number", default=DEFAULT_MAX_FILE_BYTES)

    def test_parse_int_out_of_bounds_non_interactive(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            # Below min
            with self.assertRaises(RuntimeError):
                parse_int(
                    "DOWNLOAD_TIMEOUT_SECONDS",
                    "-5",
                    default=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
                    min_value=1,
                )
            # Above max (Telegram limit 50MB)
            with self.assertRaises(RuntimeError):
                parse_int(
                    "MAX_FILE_BYTES",
                    "999999999",
                    default=DEFAULT_MAX_FILE_BYTES,
                    max_value=50 * 1024 * 1024,
                )

    def test_parse_int_invalid_interactive_accept_reset(self) -> None:
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="yes"),
        ):
            res = parse_int(
                "DOWNLOAD_TIMEOUT_SECONDS",
                "-10",
                default=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
                min_value=1,
                env_file=self.env_file,
            )
            self.assertEqual(res, DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)

    # --- parse_path tests ---

    def test_parse_path_defaults_when_empty_or_none(self) -> None:
        default_path = Path("downloads/media_cache.db")
        self.assertEqual(parse_path("CACHE_DB_PATH", None, default=default_path), default_path)
        self.assertEqual(parse_path("CACHE_DB_PATH", "  ", default=default_path), default_path)

    def test_parse_path_directory_rejected(self) -> None:
        # Existing directory passed as file path
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                parse_path(
                    "CACHE_DB_PATH",
                    self.temp_dir.name,
                    default=Path("downloads/cache.db"),
                )

    def test_load_settings_upload_timeout_default(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOW_PUBLIC": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings(env_file=self.env_file)
            self.assertEqual(settings.upload_timeout_seconds, DEFAULT_UPLOAD_TIMEOUT_SECONDS)

    def test_load_settings_upload_timeout_custom(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "UPLOAD_TIMEOUT_SECONDS": "240",
            "ALLOW_PUBLIC": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings(env_file=self.env_file)
            self.assertEqual(settings.upload_timeout_seconds, 240)

    def test_load_settings_requires_access_control(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
        }
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                load_settings(env_file=self.env_file)
            self.assertIn("ALLOWED_USER_IDS", str(ctx.exception))

    def test_load_settings_allowlist_without_public(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOWED_USER_IDS": "111,222",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings(env_file=self.env_file)
            self.assertEqual(settings.allowed_user_ids, frozenset({111, 222}))
            self.assertFalse(settings.allow_public)
            self.assertTrue(settings.https_only)

    def test_load_settings_https_only_override(self) -> None:
        env = {
            "BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            "STORAGE_CHAT_ID": "1234567890",
            "ALLOW_PUBLIC": "true",
            "HTTPS_ONLY": "false",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = load_settings(env_file=self.env_file)
            self.assertFalse(settings.https_only)

    def test_parse_user_ids_invalid_never_opens_bot(self) -> None:
        from telegram_share_bot.config import parse_user_ids

        with self.assertRaises(RuntimeError):
            parse_user_ids("ALLOWED_USER_IDS", "not-an-id")

    def test_parse_media_hosts_default_and_star(self) -> None:
        from telegram_share_bot.config import (
            DEFAULT_ALLOWED_MEDIA_HOSTS,
            parse_media_hosts,
        )

        self.assertEqual(
            parse_media_hosts("ALLOWED_MEDIA_HOSTS", None),
            DEFAULT_ALLOWED_MEDIA_HOSTS,
        )
        self.assertIsNone(parse_media_hosts("ALLOWED_MEDIA_HOSTS", "*"))
        self.assertEqual(
            parse_media_hosts("ALLOWED_MEDIA_HOSTS", "Example.COM, youtube.com"),
            frozenset({"example.com", "youtube.com"}),
        )


if __name__ == "__main__":
    unittest.main()
