"""Logging filters that keep secrets out of log output."""

from __future__ import annotations

import logging
import re
from typing import Any

# httpx logs: HTTP Request: POST https://api.telegram.org/bot<TOKEN>/<method> "..."
_TELEGRAM_BOT_URL_RE = re.compile(
    r"https://api\.telegram\.org/bot[^/\s]+/([A-Za-z0-9_]+)"
)


def _redact_telegram_urls(value: str) -> str:
    """Replace full Telegram Bot API URLs with the method name only."""
    return _TELEGRAM_BOT_URL_RE.sub(r"\1", value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_telegram_urls(value)
    return value


class RedactTelegramBotUrlFilter(logging.Filter):
    """Rewrite Telegram Bot API URLs in log records to the endpoint name only."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_value(val) for key, val in record.args.items()}
        return True
