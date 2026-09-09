"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

# Stay under Telegram Bot API's ~50 MB upload limit.
DEFAULT_MAX_FILE_BYTES = 45 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 90


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    storage_chat_id: int
    max_file_bytes: int
    download_timeout_seconds: int
    download_dir: Path


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token.startswith("123456:"):
        raise RuntimeError(
            "Set BOT_TOKEN in .env (create a bot with @BotFather, then /setinline)."
        )

    storage_raw = os.getenv("STORAGE_CHAT_ID", "").strip()
    if not storage_raw or storage_raw == "123456789":
        raise RuntimeError(
            "Set STORAGE_CHAT_ID in .env to a chat the bot can write to "
            "(your user id after /start works)."
        )

    try:
        storage_chat_id = int(storage_raw)
    except ValueError as exc:
        raise RuntimeError("STORAGE_CHAT_ID must be an integer.") from exc

    max_file_bytes = int(os.getenv("MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES)))
    download_timeout = int(
        os.getenv("DOWNLOAD_TIMEOUT_SECONDS", str(DEFAULT_DOWNLOAD_TIMEOUT_SECONDS))
    )

    download_dir = _ROOT / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=token,
        storage_chat_id=storage_chat_id,
        max_file_bytes=max_file_bytes,
        download_timeout_seconds=download_timeout,
        download_dir=download_dir,
    )
