"""User-facing and operator-facing copy.

Edit this file to reword messages or prepare for translation later.
No locale switching is implemented — keep one language here.
"""

from __future__ import annotations

# --- Branding / defaults -----------------------------------------------------

BOT_DISPLAY_NAME = "telegram-share-bot"
FALLBACK_BOT_USERNAME = "YourBot"
EXAMPLE_MEDIA_URL = "https://example.com/video"

# --- Access control ----------------------------------------------------------

ACCESS_DENIED = "You are not authorized to use this bot."
INLINE_ACCESS_DENIED_TITLE = "Access denied"
INLINE_ACCESS_DENIED_DESCRIPTION = "You are not authorized to use this bot."

START_MESSAGE = (
    "Welcome to <b>{bot_name}</b>!\n\n"
    "I download media from YouTube, Twitter/X, and similar links, "
    "then send the file in chat.\n\n"
    "Try me inline:\n"
    "<code>@{bot_username} {example_url}</code>\n\n"
    "Or paste a media URL here."
)

# --- Direct private-chat URL handling ----------------------------------------

DIRECT_URL_HINT = "Send a media URL, or use me inline: @BotName <url>"
DIRECT_DOWNLOADING = "Downloading…"
DIRECT_DONE = "Done."
DIRECT_DOWNLOAD_FAILED = "Could not download: {error}"
DIRECT_SEND_FAILED = "Something went wrong while sending the media."

# --- Inline query (fast answer) ----------------------------------------------

INLINE_EMPTY_TITLE = "Paste a media URL"
INLINE_EMPTY_DESCRIPTION = (
    "Type a YouTube, Twitter/X, etc. link after the bot username."
)

INLINE_NO_URL_TITLE = "No URL found"
INLINE_NO_URL_DESCRIPTION = "Include a full http(s) link in the inline query."

INLINE_PENDING_TITLE = "Send media"
INLINE_PENDING_MESSAGE = "Preparing media…\n{url}"
INLINE_CANCEL_BUTTON = "Cancel"
INLINE_CANCELLED = "Cancelled."
INLINE_CANCEL_ANSWER = "Cancelled"

# --- After user chooses an inline result -------------------------------------

INLINE_CHOSEN_NO_URL = "No URL found in the query."
INLINE_CHOSEN_DOWNLOADING = "Downloading…\n{url}"
INLINE_CHOSEN_DOWNLOAD_FAILED = "Download failed\n\n{error}"
INLINE_CHOSEN_PREPARE_FAILED = "Something went wrong while preparing the media."

# --- Download errors (shown to users) ----------------------------------------

DOWNLOAD_NO_FILE = "Download finished but no file was found."
DOWNLOAD_EXTRACT_FAILED = "Could not extract media from that URL."
DOWNLOAD_PLAYLIST_UNSUPPORTED = "Playlist/empty result is not supported."
DOWNLOAD_EMPTY_FILE = "Downloaded file is empty."
DOWNLOAD_TOO_LARGE = (
    "File is too large ({size_mb} MB). Max is {max_mb} MB."
)
DOWNLOAD_EXCEEDS_LIMIT = "File exceeds the {max_mb} MB limit."
DOWNLOAD_FAILED_GENERIC = "Download failed."
DOWNLOAD_FAILED_WITH_DETAIL = "Download failed: {error}"
DOWNLOAD_TIMED_OUT = "Download timed out after {timeout_seconds} seconds."
DOWNLOAD_UNSAFE_URL = "Unsafe or internal URLs are not allowed."
DOWNLOAD_FAILED_INCOMPLETE = (
    "Download was aborted or incomplete (file may exceed limits)."
)
DOWNLOAD_LIVE_UNSUPPORTED = "Live streams cannot be downloaded."

# --- Config / startup (operator-facing) --------------------------------------

CONFIG_MISSING_BOT_TOKEN = (
    "Set BOT_TOKEN in .env (create a bot with @BotFather, then /setinline)."
)
CONFIG_MISSING_STORAGE_CHAT_ID = (
    "Set STORAGE_CHAT_ID in .env to a chat the bot can write to "
    "(your user id after /start works)."
)
CONFIG_STORAGE_CHAT_ID_NOT_INT = "STORAGE_CHAT_ID must be an integer."
STARTUP_POLLING = "Starting telegram-share-bot (polling)"

# --- Internal errors that may surface to users -------------------------------

TELEGRAM_NO_FILE_ID = "Telegram did not return a usable file_id."
