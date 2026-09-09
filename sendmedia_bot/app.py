"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging

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

from sendmedia_bot import strings
from sendmedia_bot.cache import MediaCache
from sendmedia_bot.config import DEFAULT_UPLOAD_TIMEOUT_SECONDS, load_settings
from sendmedia_bot.handlers import (
    cancel_callback,
    chosen_inline_result,
    help_command,
    inline_query,
    start_command,
    url_message,
)
from sendmedia_bot.logging_filters import configure_logging

App = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]


async def _post_init(application: App) -> None:
    """Ensure the bot's identity is fetched if startup encountered a transient network timeout."""
    if application.bot._bot_user is None:
        for attempt in range(1, 4):
            try:
                await application.bot.get_me()
                break
            except Exception:
                if attempt == 3:
                    raise
                await asyncio.sleep(1.0)


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
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["media_cache"] = MediaCache(settings.cache_db_path)
    raw_concurrency = getattr(settings, "max_concurrent_downloads", 3)
    try:
        concurrency = int(raw_concurrency)
    except (TypeError, ValueError):
        concurrency = 3
    application.bot_data["download_semaphore"] = asyncio.Semaphore(concurrency)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    application.add_handler(
        CallbackQueryHandler(cancel_callback, pattern=r"^cancel:")
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
