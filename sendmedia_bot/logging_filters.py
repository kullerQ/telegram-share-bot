"""Logging helpers that keep secrets out of log output."""

from __future__ import annotations

import logging
import re
from typing import Any

# httpx logs: HTTP Request: POST https://api.telegram.org/bot<TOKEN>/<method> "..."
# request.url is an httpx.URL object, not a str — redact via str(value).
_TELEGRAM_BOT_URL_RE = re.compile(
    r"https://api\.telegram\.org/bot[^/\s]+/([A-Za-z0-9_]+)"
)
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def _redact_telegram_secrets(value: str) -> str:
    """Replace Bot API URLs with the method name; mask bare tokens."""
    text = _TELEGRAM_BOT_URL_RE.sub(r"\1", value)
    return _TELEGRAM_BOT_TOKEN_RE.sub("<redacted-bot-token>", text)


def _redact_value(value: Any) -> Any:
    text = value if isinstance(value, str) else str(value)
    redacted = _redact_telegram_secrets(text)
    if isinstance(value, str):
        return redacted
    # Replace non-str args (e.g. httpx.URL) when redaction changed the text.
    if redacted != text:
        return redacted
    return value


class RedactTelegramBotUrlFilter(logging.Filter):
    """Rewrite Telegram Bot API URLs/tokens in log record fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_value(val) for key, val in record.args.items()}
        return True


class RedactTelegramBotUrlFormatter(logging.Formatter):
    """Final-pass redaction after the message is fully formatted."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_telegram_secrets(super().format(record))


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging with Telegram secret redaction."""
    root = logging.getLogger()
    root.setLevel(level)

    formatter = RedactTelegramBotUrlFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    redact_filter = RedactTelegramBotUrlFilter()

    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redact_filter)
        root.addHandler(stream_handler)
    else:
        for existing in root.handlers:
            existing.setFormatter(formatter)
            existing.addFilter(redact_filter)

    logging.getLogger("httpx").addFilter(redact_filter)
