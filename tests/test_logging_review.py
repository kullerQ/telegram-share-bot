"""Focused checks for secret-safe operational log formatting."""

from __future__ import annotations

import logging
import unittest

import httpx

from telegram_share_bot.logging_filters import (
    RedactTelegramBotUrlFilter,
    RedactTelegramBotUrlFormatter,
)


class TestLogRedaction(unittest.TestCase):
    def test_filter_and_formatter_hide_tokens_and_all_url_queries(self) -> None:
        token = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFGHI"
        record = logging.LogRecord(
            "httpx",
            logging.WARNING,
            __file__,
            1,
            "request %s token %s video https://youtube.com/watch?v=private-id&si=secret",
            (httpx.URL(f"https://api.telegram.org/bot{token}/sendVideo?key=secret"), token),
            None,
        )
        self.assertTrue(RedactTelegramBotUrlFilter().filter(record))
        message = RedactTelegramBotUrlFormatter("%(message)s").format(record)
        self.assertIn("sendVideo", message)
        self.assertIn("<redacted-bot-token>", message)
        self.assertNotIn(token, message)
        self.assertNotIn("?", message)
        self.assertNotIn("private-id", message)
        self.assertNotIn("secret", message)


if __name__ == "__main__":
    unittest.main()
