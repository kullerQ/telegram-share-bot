"""Application entrypoint."""

from __future__ import annotations

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
from sendmedia_bot.config import load_settings
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
    )


if __name__ == "__main__":
    main()
