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
RATE_LIMITED = "Too many downloads in progress. Please wait and try again."
COOLDOWN_LIMITED = "Please wait a moment before requesting another download."
INLINE_RATE_LIMITED_TITLE = "Busy"
INLINE_RATE_LIMITED_DESCRIPTION = "Too many downloads in progress. Please wait."

START_MESSAGE = (
    "Welcome to {bot_name}!\n\n"
    "Share a media link in another chat with the button below, or paste it here.\n"
    "Use /help for link formats and YouTube clip options."
)
START_SHARE_MEDIA_BUTTON = "Share media"
HELP_MESSAGE = (
    "Send one media link in this chat, or tap Share media to choose another chat.\n\n"
    "Inline: type @{bot_username} followed by a media link.\n"
    "Private chat: paste the link directly.\n"
    "Add optional caption text after the link.\n\n"
    "YouTube clips: add a range such as 1:20-2:05, or add a start time and "
    "duration in seconds, for example ?t=2022 30. A start time alone selects "
    "the video through its end. You can choose the clip or the full video.\n\n"
    "Share a link to one media item. Playlists and multi-item collections are "
    "not supported."
)

# --- Direct private-chat URL handling ----------------------------------------

DIRECT_URL_HINT = "Send a media URL, or use me inline: @BotName <url>"
DIRECT_PREPARING = "Checking the media link…"
DIRECT_DOWNLOADING = "Downloading media…"
DIRECT_UPLOADING = "Sending media to Telegram…"
DIRECT_DONE = "Done."
DIRECT_DOWNLOAD_FAILED = "Could not download that media."
DIRECT_SEND_FAILED = "Something went wrong while sending the media."
DIRECT_UPLOAD_FAILED = (
    "Could not upload the media to Telegram (network error). Please try again."
)
DIRECT_CLIP_PROMPT = (
    "YouTube time range detected ({range_label}).\n"
    "Choose clip or full video:"
)
DIRECT_CLIP_EXPIRED = "That choice expired. Send the link again."
DIRECT_CLIP_CHOICE_ANSWER = "Got it"

# --- Inline query (fast answer) ----------------------------------------------

INLINE_EMPTY_TITLE = "Paste a media URL"
INLINE_EMPTY_DESCRIPTION = (
    "Type a YouTube, Twitter/X, etc. link after the bot username."
)

INLINE_NO_URL_TITLE = "No URL found"
INLINE_NO_URL_DESCRIPTION = "Include a full http(s) link in the inline query."

INLINE_PENDING_TITLE = "Send media"
INLINE_PENDING_CLIP_TITLE = "Send clip {range_label}"
INLINE_PENDING_FULL_TITLE = "Send full video"
INLINE_PENDING_MESSAGE = "Preparing media…\n{url}"
INLINE_PENDING_CLIP_MESSAGE = "Preparing clip {range_label}…\n{url}"
INLINE_CANCEL_BUTTON = "Cancel"
INLINE_CANCELLED = "Cancelled."
INLINE_CANCEL_ANSWER = "Cancelled"
INLINE_RETRY_BUTTON = "Try again"
INLINE_RETRY_CLIP_BUTTON = "Retry clip"
INLINE_SEND_FULL_BUTTON = "Send full video"
INLINE_RETRY_ANSWER = "Retrying…"
INLINE_RETRY_EXPIRED = "This request has expired. Send the link again."
INLINE_ALREADY_PREPARING = "This media is already being prepared."

# --- After user chooses an inline result -------------------------------------

INLINE_CHOSEN_NO_URL = "No URL found in the query."
INLINE_CHOSEN_DOWNLOADING = "Checking the media link…\n{url}"
INLINE_DOWNLOADING = "Downloading media…\n{url}"
INLINE_UPLOADING = "Sending media to Telegram…\n{url}"
INLINE_CHOSEN_DOWNLOAD_FAILED = "Download failed."
INLINE_CHOSEN_UPLOAD_FAILED = (
    "Could not upload the media to Telegram (network error). Please try again."
)
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
DOWNLOAD_TIMED_OUT = "Download timed out after {timeout_seconds} seconds."
DOWNLOAD_UNSAFE_URL = "Unsafe or internal URLs are not allowed."
DOWNLOAD_HOST_NOT_ALLOWED = "That media host is not allowed."
DOWNLOAD_HTTPS_REQUIRED = "Only HTTPS media URLs are allowed."
DOWNLOAD_FAILED_INCOMPLETE = (
    "Download was aborted or incomplete (file may exceed limits)."
)
DOWNLOAD_LIVE_UNSUPPORTED = "Live streams cannot be downloaded."
DOWNLOAD_CLIP_TOO_LONG = (
    "Clips longer than {max_minutes} minutes are not supported. "
    "Choose full video, or use a shorter range."
)
DOWNLOAD_CLIP_OUT_OF_BOUNDS = "That time range is outside the video length."
DOWNLOAD_CLIP_FFMPEG_MISSING = (
    "ffmpeg is required to download YouTube clips."
)
SLIDESHOW_FFMPEG_MISSING = (
    "ffmpeg is required to compile TikTok photo posts into video."
)
SLIDESHOW_NO_IMAGES = "No images found in that TikTok photo post."
SLIDESHOW_BUILD_FAILED = "Could not build a slideshow video from that photo post."

# --- Config / startup (operator-facing) --------------------------------------

CONFIG_MISSING_BOT_TOKEN = (
    "Set BOT_TOKEN in .env (create a bot with @BotFather, then /setinline)."
)
CONFIG_MISSING_STORAGE_CHAT_ID = (
    "Set STORAGE_CHAT_ID in .env to a private chat the bot can write to "
    "(your user id after /start works). Do not use a shared group/channel."
)
CONFIG_STORAGE_CHAT_ID_NOT_INT = "STORAGE_CHAT_ID must be an integer."
CONFIG_MISSING_ACCESS_CONTROL = (
    "Set ALLOWED_USER_IDS to a comma-separated list of Telegram user ids, "
    "or set ALLOW_PUBLIC=true to intentionally allow everyone."
)
CONFIG_STORAGE_CHAT_NOT_PRIVATE = (
    "STORAGE_CHAT_ID must be a private chat (your user id). "
    "For a shared group/channel set ALLOW_SHARED_STORAGE=true "
    "(not recommended — downloaded media is visible there)."
)
CONFIG_STORAGE_CHAT_UNREACHABLE = (
    "Bot cannot access STORAGE_CHAT_ID. Open a private chat with the bot "
    "and /start, or fix the id."
)
STARTUP_POLLING = "Starting telegram-share-bot (polling)"

# --- Internal errors that may surface to users -------------------------------

TELEGRAM_NO_FILE_ID = "Telegram did not return a usable file_id."
