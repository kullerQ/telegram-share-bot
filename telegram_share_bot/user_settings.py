"""Persistent per-user sharing preferences."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from telegram_share_bot.downloader import MediaFormat, VideoQualityPolicy


class CaptionPreference(str, Enum):
    MEDIA_TITLE = "media-title"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class UserSharingSettings:
    video_quality: VideoQualityPolicy = VideoQualityPolicy.AUTO
    caption: CaptionPreference | None = None
    default_format: MediaFormat | None = None


class UserSettingsStore:
    """SQLite store isolated from downloaded files and Telegram's media cache."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            with conn:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA busy_timeout = 5000;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_settings (
                        user_id INTEGER PRIMARY KEY,
                        video_quality TEXT NOT NULL DEFAULT 'auto-best',
                        caption TEXT,
                        default_format TEXT,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

    def _get_sync(self, user_id: int) -> UserSharingSettings:
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            row = conn.execute(
                "SELECT video_quality, caption, default_format "
                "FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return UserSharingSettings()
        try:
            quality = VideoQualityPolicy(row[0])
            # Preserve preferences saved while this branch was being tested.
            legacy_caption = {"original-link": "media-title", "none": "custom"}.get(row[1], row[1])
            caption = CaptionPreference(legacy_caption) if legacy_caption is not None else None
            media_format = MediaFormat(row[2]) if row[2] is not None else None
        except ValueError:
            # Recover safely from manually edited or older malformed rows.
            return UserSharingSettings()
        return UserSharingSettings(quality, caption, media_format)

    def _set_sync(
        self,
        user_id: int,
        *,
        video_quality: VideoQualityPolicy | None = None,
        caption: CaptionPreference | None = None,
        reset_caption: bool = False,
        default_format: MediaFormat | None = None,
        reset_format: bool = False,
    ) -> UserSharingSettings:
        if (
            video_quality is None
            and caption is None
            and not reset_caption
            and default_format is None
            and not reset_format
        ):
            return self._get_sync(user_id)
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            with conn:
                conn.execute("PRAGMA busy_timeout = 5000;")
                conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
                if video_quality is not None:
                    conn.execute(
                        "UPDATE user_settings SET video_quality = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (video_quality.value, user_id),
                    )
                if caption is not None or reset_caption:
                    conn.execute(
                        "UPDATE user_settings SET caption = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (caption.value if caption is not None else None, user_id),
                    )
                if default_format is not None or reset_format:
                    conn.execute(
                        "UPDATE user_settings SET default_format = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (default_format.value if default_format is not None else None, user_id),
                    )
        return self._get_sync(user_id)

    async def get(self, user_id: int) -> UserSharingSettings:
        return await asyncio.to_thread(self._get_sync, user_id)

    async def set_quality(self, user_id: int, quality: VideoQualityPolicy) -> UserSharingSettings:
        return await asyncio.to_thread(self._set_sync, user_id, video_quality=quality)

    async def set_caption(
        self, user_id: int, caption: CaptionPreference | None
    ) -> UserSharingSettings:
        return await asyncio.to_thread(
            self._set_sync,
            user_id,
            caption=caption,
            reset_caption=caption is None,
        )

    async def set_format(
        self, user_id: int, media_format: MediaFormat | None
    ) -> UserSharingSettings:
        return await asyncio.to_thread(
            self._set_sync,
            user_id,
            default_format=media_format,
            reset_format=media_format is None,
        )

    def _reset_sync(self, user_id: int) -> UserSharingSettings:
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            with conn:
                conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        return UserSharingSettings()

    async def reset(self, user_id: int) -> UserSharingSettings:
        return await asyncio.to_thread(self._reset_sync, user_id)
