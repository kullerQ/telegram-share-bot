# telegram-share-bot

Lightweight Telegram **inline** bot: type `@YourBot <media-url>` in any chat and send the downloaded media as the result.

Works in private chats, groups, and channels. The bot does **not** need to be a member of the target chat.

## BotFather setup (required)

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, choose a display name and a username ending in `bot`.
3. Copy the bot token into `.env` as `BOT_TOKEN`.
4. Enable inline mode:
   - `/setinline` → select your bot → set placeholder text, e.g. `Paste a media URL…`
5. **Enable inline feedback** (required for downloads to finish after you tap a result):
   - `/setinlinefeedback` → select your bot → enable (100% is fine)
6. Optional:
   - `/setdescription` and `/setabouttext` for store listing text

Without `/setinline`, the bot will not appear when users type `@YourBot`.
Without `/setinlinefeedback`, the bot cannot learn which result you chose, so the media never replaces the placeholder.

## Docker setup (Recommended)

1. Configure `.env`:
   ```bash
   cp .env.example .env
   # Edit .env and set BOT_TOKEN and STORAGE_CHAT_ID
   ```

2. Create host dirs and fix ownership (container runs as uid/gid `1000`):
   ```bash
   mkdir -p downloads logs
   sudo chown -R 1000:1000 ./downloads ./logs
   ```
   On Windows this step is usually unnecessary; on Linux bind mounts owned by `root` will cause `PermissionDenied` when the bot writes downloads or logs.

3. Start the container in the background:
   ```bash
   docker compose up -d
   ```

4. **Accessing logs**:
   - Live stream in console:
     ```bash
     docker compose logs -f
     ```
   - Direct file access on host:
     Open `./logs/bot.log` in your editor. Sensitive bot tokens are automatically redacted.
   - Health: `docker compose ps` should show the bot as `healthy` after startup.

5. Stop or restart:
   ```bash
   docker compose restart
   docker compose down
   ```
## Local setup

```powershell
# from the project root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
git config core.hooksPath .githooks
copy .env.example .env
```

`core.hooksPath` points Git at the repo’s `.githooks/pre-commit` script (LF line endings — required on Windows). That hook runs **ruff** and **mypy --strict -p telegram_share_bot** (same as CI) before each commit. Run `pre-commit run --all-files` to check everything without committing.

If a commit seems to skip checks, make sure Cursor’s commit UI does **not** have “Skip hook verification” / `--no-verify` enabled.

Edit `.env`:

1. Set `BOT_TOKEN` from BotFather.
2. Set `STORAGE_CHAT_ID` to a chat the bot can write to (your user id works after you `/start` the bot once).

Run:

```powershell
python -m telegram_share_bot
```

Open a private chat with the bot, send `/start`, then in any other chat type:

```text
@YourBot https://youtube.com/watch?v=…
```

Tap **Send media**. A placeholder appears first; the bot downloads in the background and replaces it with the file. Tap **Cancel** to stop and clear the placeholder.

## How it works

1. Telegram sends an `inline_query` with the URL.
2. The bot answers **immediately** with a placeholder article (Telegram rejects answers that take too long).
3. When you tap the result, Telegram sends `chosen_inline_result` (needs `/setinlinefeedback`).
4. The bot downloads via `yt-dlp`, uploads to `STORAGE_CHAT_ID` for a `file_id`, deletes that storage message, then edits the inline message to the media.

## Limits

- Max file size ≈ 45 MB (Telegram Bot API upload limit is 50 MB).
- Download timeout defaults to 90 seconds.
- Unsupported or oversize URLs replace the placeholder with an error message.

## Notes

- Users should `/start` the bot once before relying on inline mode.
- Respect platform Terms of Service for downloaded content; this project is for personal/lightweight use.
