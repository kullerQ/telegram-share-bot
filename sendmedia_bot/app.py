"""Application entrypoint."""

from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ExtBot,
    InlineQueryHandler,
    JobQueue,
    MessageHandler,
    filters,
)

from sendmedia_bot.config import load_settings
from sendmedia_bot.handlers import (
    help_command,
    inline_query,
    start_command,
    url_message,
)
from sendmedia_bot.logging_filters import RedactTelegramBotUrlFilter

App = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]


def build_application() -> App:
    settings = load_settings()
    application: App = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            url_message,
        )
    )
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    redact_filter = RedactTelegramBotUrlFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact_filter)
    # Also cover httpx if it has its own handlers later.
    logging.getLogger("httpx").addFilter(redact_filter)

    application = build_application()
    logging.getLogger(__name__).info("Starting SendMedia bot (polling)")
    application.run_polling(allowed_updates=["message", "inline_query"])


if __name__ == "__main__":
    main()
