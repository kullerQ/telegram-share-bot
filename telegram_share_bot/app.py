"""Application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    InlineQueryHandler,
    JobQueue,
    MessageHandler,
    filters,
)

from telegram_share_bot import strings
from telegram_share_bot.config import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    Settings,
    load_settings,
)
from telegram_share_bot.handlers import (
    audio_command,
    cancel_callback,
    chosen_inline_result,
    clip_choice_callback,
    direct_format_callback,
    help_command,
    inline_query,
    private_choice_cancel_callback,
    retry_inline_callback,
    settings_callback,
    settings_command,
    start_command,
    url_message,
    video_command,
)
from telegram_share_bot.logging_filters import configure_logging
from telegram_share_bot.media.jobs import cleanup_stale_downloads
from telegram_share_bot.media.work import MediaWorkSupervisor
from telegram_share_bot.storage.media_cache import MediaCache
from telegram_share_bot.storage.user_settings import UserSettingsStore

App = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]

_MEDIA_MAINTENANCE_TASK = "media_maintenance_task"
_MEDIA_MAINTENANCE_STOP = "media_maintenance_stop"
_MEDIA_MAINTENANCE_INTERVAL_SECONDS = 300


async def _validate_storage_chat(application: App, settings: Settings) -> None:
    """Ensure STORAGE_CHAT_ID is reachable and private unless opted out."""
    try:
        chat = await application.bot.get_chat(settings.storage_chat_id)
    except TelegramError as exc:
        raise RuntimeError(strings.CONFIG_STORAGE_CHAT_UNREACHABLE) from exc

    if settings.allow_shared_storage:
        return
    if chat.type != ChatType.PRIVATE:
        raise RuntimeError(strings.CONFIG_STORAGE_CHAT_NOT_PRIVATE)


async def _maintain_downloads(download_dir: Path, stop_event: asyncio.Event) -> None:
    """Periodically remove abandoned work directories without blocking updates."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_MEDIA_MAINTENANCE_INTERVAL_SECONDS
            )
        except TimeoutError:
            try:
                removed = await asyncio.to_thread(cleanup_stale_downloads, download_dir)
                if removed:
                    logging.getLogger(__name__).info(
                        "Removed %s stale download director%s",
                        removed,
                        "y" if removed == 1 else "ies",
                    )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Periodic download cleanup failed", exc_info=True
                )


async def _post_init(application: App) -> None:
    """Run startup housekeeping and fetch bot identity if needed."""
    settings = application.bot_data.get("settings")
    download_dir = getattr(settings, "download_dir", None)
    if isinstance(download_dir, Path):
        try:
            removed = await asyncio.to_thread(cleanup_stale_downloads, download_dir)
        except Exception:
            logging.getLogger(__name__).warning(
                "Startup download cleanup failed", exc_info=True
            )
            removed = 0
        if removed:
            logging.getLogger(__name__).info(
                "Removed %s stale download director%s on startup",
                removed,
                "y" if removed == 1 else "ies",
            )

    if application.bot._bot_user is None:
        for attempt in range(1, 4):
            try:
                await application.bot.get_me()
                break
            except Exception:
                if attempt == 3:
                    raise
                await asyncio.sleep(1.0)

    if isinstance(settings, Settings):
        await _validate_storage_chat(application, settings)
    if isinstance(download_dir, Path):
        stop_event = asyncio.Event()
        application.bot_data[_MEDIA_MAINTENANCE_STOP] = stop_event
        application.bot_data[_MEDIA_MAINTENANCE_TASK] = asyncio.create_task(
            _maintain_downloads(download_dir, stop_event),
            name="media-download-maintenance",
        )


async def _post_shutdown(application: App) -> None:
    stop_event = application.bot_data.get(_MEDIA_MAINTENANCE_STOP)
    task = application.bot_data.get(_MEDIA_MAINTENANCE_TASK)
    if isinstance(stop_event, asyncio.Event):
        stop_event.set()
    if isinstance(task, asyncio.Task):
        with contextlib.suppress(asyncio.CancelledError):
            await task


def build_application() -> App:
    settings = load_settings()
    raw_upload_timeout = getattr(settings, "upload_timeout_seconds", DEFAULT_UPLOAD_TIMEOUT_SECONDS)
    try:
        upload_timeout = float(raw_upload_timeout)
    except (TypeError, ValueError):
        upload_timeout = float(DEFAULT_UPLOAD_TIMEOUT_SECONDS)

    application: App = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(20.0)
        .media_write_timeout(upload_timeout)
        .pool_timeout(5.0)
        .get_updates_connect_timeout(15.0)
        .get_updates_read_timeout(30.0)
        .get_updates_write_timeout(20.0)
        .get_updates_pool_timeout(5.0)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["media_cache"] = MediaCache(settings.cache_db_path)
    user_settings_db_path = getattr(settings, "user_settings_db_path", None)
    if isinstance(user_settings_db_path, Path):
        application.bot_data["user_settings"] = UserSettingsStore(user_settings_db_path)
    raw_concurrency = getattr(
        settings, "max_concurrent_downloads", DEFAULT_MAX_CONCURRENT_DOWNLOADS
    )
    try:
        concurrency = int(raw_concurrency)
    except (TypeError, ValueError):
        concurrency = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    # 0 preserves the documented unlimited-parallelism opt-out.
    application.bot_data["media_work_supervisor"] = MediaWorkSupervisor(
        concurrency,
        max_waiters=4 * concurrency if concurrency > 0 else 0,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        CommandHandler("settings", settings_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^settings:"))
    application.add_handler(
        CommandHandler("audio", audio_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("video", video_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    application.add_handler(
        CallbackQueryHandler(retry_inline_callback, pattern=r"^(retry|fallback):")
    )
    application.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    application.add_handler(
        CallbackQueryHandler(private_choice_cancel_callback, pattern=r"^direct-cancel:")
    )
    application.add_handler(
        CallbackQueryHandler(
            direct_format_callback,
            pattern=r"^(video|video-best|video-balanced|audio):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            clip_choice_callback,
            pattern=r"^(clipaudio|fullaudio|clip|full|clip-best|clip-balanced|full-best|full-balanced):",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            url_message,
        )
    )
    return application


def main() -> None:
    configure_logging()
    application = build_application()
    logging.getLogger(__name__).info(strings.STARTUP_POLLING)
    application.run_polling(
        allowed_updates=[
            "message",
            "inline_query",
            "chosen_inline_result",
            "callback_query",
        ],
        drop_pending_updates=True,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
