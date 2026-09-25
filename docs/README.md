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
   # Edit .env: BOT_TOKEN, STORAGE_CHAT_ID, and ALLOWED_USER_IDS (or ALLOW_PUBLIC=true)
   ```

   **Access control is mandatory.** Set `ALLOWED_USER_IDS` to your Telegram user id(s), or explicitly set `ALLOW_PUBLIC=true` if you intend to run an open downloader.

   **Storage chat privacy:** set `STORAGE_CHAT_ID` to your private user id (or a private channel only you can read). Do not use a shared group — every downloaded file is briefly uploaded there. The bot refuses non-private storage chats unless `ALLOW_SHARED_STORAGE=true`.

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

   Compose drops Linux capabilities, runs a read-only root filesystem (with writable `downloads`/`logs` mounts and `/tmp`), and health-checks reachability of `api.telegram.org`. For stronger SSRF defense, also restrict the host/container egress firewall to HTTPS destinations you trust (Telegram API + media CDNs).

4. **Accessing logs**:
   - Live stream in console:
     ```bash
     docker compose logs -f
     ```
   - Direct file access on host:
     Open `./logs/bot.log` in your editor. Bot tokens and URL query strings are automatically redacted.
   - Health: `docker compose ps` should show the bot as `healthy` after startup.

5. Stop or restart:
   ```bash
   docker compose restart
   docker compose down
   ```

6. Update to the latest image:
   ```bash
   docker compose pull
   docker compose up -d
   ```
   `pull` fetches the new image; `up -d` recreates the container if the image changed. To apply `.env` changes without pulling, use `docker compose up -d --force-recreate --pull never`.

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

Edit `.env`:

1. Set `BOT_TOKEN` from BotFather.
2. Set `STORAGE_CHAT_ID` to a **private** chat the bot can write to (your user id works after you `/start` the bot once).
3. Set `ALLOWED_USER_IDS` to your Telegram user id (comma-separated for multiple). Or set `ALLOW_PUBLIC=true` only if you want an open bot.

Run:

```powershell
python -m telegram_share_bot
```

Open a private chat with the bot, send `/start`, then in any other chat type:

```text
@YourBot https://youtube.com/watch?v=…
```

With `CAPTION_MODE=custom`, append a caption after the link:

```text
@YourBot https://youtube.com/watch?v=… optional caption text
```

For a YouTube **clip**, either:

- put an absolute time range as the first token after the link (`start-end`, e.g. `1:20-2:05` or `90-150`), or
- use a share link with `?t=` / `start=` and a following duration in **seconds** (e.g. `?t=2022` + `30` → clip from 33:42 for 30 seconds), or
- use `?t=` / `start=` alone to choose a clip from that start through the end of the video (still offers full video as the other choice).

Inline mode offers Video and Audio results; YouTube clip links also offer clip and full-length versions in both formats. In a private chat, paste a link and choose Video or Audio. For a direct choice, send `/video <link>` or `/audio <link>`. Captions may follow the range or duration. A caption without a leading range/duration on a link without `t=` still downloads the whole video.

```text
@YourBot https://youtube.com/watch?v=… 1:20-2:05
@YourBot https://youtube.com/watch?v=… 1:20-2:05 optional caption
@YourBot https://youtu.be/…?t=2022 30
@YourBot https://youtu.be/…?t=2022 30 optional caption
@YourBot https://youtu.be/…?t=2022
```

Tap a Video or Audio result (or a clip / full-length choice). A placeholder appears first; the bot prepares the media in the background and replaces it with the file. Tap **Cancel** to stop and clear the placeholder.

## How it works

1. Telegram sends an `inline_query` with the URL.
2. The bot answers **immediately** with a placeholder article (Telegram rejects answers that take too long).
3. When you tap the result, Telegram sends `chosen_inline_result` (needs `/setinlinefeedback`).
4. The bot downloads via `yt-dlp`, uploads to `STORAGE_CHAT_ID` for a `file_id`, deletes that storage message, then edits the inline message to the media. Video selection prefers the best plausible quality that fits the configured Telegram size limit, tries lower-quality formats if the measured file is still too large, then makes at most two bounded `ffmpeg` optimization attempts. This is a size-based choice, not a fixed resolution cap. Audio selection downloads an audio-only stream and prepares native Telegram audio; YouTube clips and TikTok slideshow soundtracks are supported when the source provides audio. Telegram direct URL imports are attempted only for known-size streams within the limit and fall back to a local download if Telegram rejects the URL.

## Limits

- Max file size ≈ 45 MB (Telegram Bot API upload limit is 50 MB). Video quality is adapted to fit this limit rather than capped at one fixed resolution.
- Download timeout defaults to 90 seconds.
- Global concurrent downloads default to 3; per-user in-flight downloads default to 3. Set either to `0` to disable that limit.
- User URLs are limited to YouTube / X / Instagram / TikTok by default (`ALLOWED_MEDIA_HOSTS=*` allows any host).
- TikTok (including `vm.tiktok.com` / `vt.tiktok.com` short links) needs `curl-cffi` for browser impersonation — it is pinned in `requirements.txt`.
- TikTok **photo posts** (image slideshows with sound) can be sent as a slideshow video or as the original soundtrack when present. Video slideshows are compiled into MP4 via `ffmpeg`: each image is shown for about `SLIDESHOW_SLIDE_MS` (default 2500 ms). `SLIDESHOW_IMAGES_LOOP=true` (default) makes the video match the **full** audio track and loops images to fill it; `false` shows each image once then trims the audio (a single-image post still uses the full audio). Multi-image posts get TikTok-style page dots at the bottom. At most `SLIDESHOW_MAX_IMAGES` (default 35) images are included. The Docker image already ships a static `ffmpeg`.
- `HTTPS_ONLY` defaults to true (set `false` to allow plain `http://` media URLs).
- Captions: `CAPTION_MODE=media` (default, media title), `custom` (only text after the URL), or `off` (no captions). Custom captions are plain text, max 1024 characters, not stored in the media cache, and not sent to `STORAGE_CHAT_ID`.
- YouTube clips: optional `start-end` after the link, or `?t=` / `start=` on the URL plus a duration in seconds, or `?t=` alone (from start to end). Max clip length 10 minutes. Requires `ffmpeg` (already in the Docker image).
- Unsupported or oversize URLs replace the placeholder with an error message.

## Notes

- Users should `/start` the bot once before relying on inline mode.
- Respect platform Terms of Service for downloaded content; this project is for personal/lightweight use.
- Signed / credentialed URLs are not stored in the shared media cache.
