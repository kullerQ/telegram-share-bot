# Telegram share bot implementation plan

This plan turns the local product roadmap into themed branches. Each branch has several focused commits, one PR, and one numbered GHCR release. The versions below are an illustrative sequence from the first `1.0.0` release; choose the final major/minor/patch bump when reviewing each PR's actual user impact. The [release guide](release-versioning.md) defines that decision.

Write every authored commit message in the repository’s existing `type: concise imperative description` style (for example, `feat: add audio sharing` or `fix: remove stale Cancel button`). The [release guide](release-versioning.md#commit-message-style) records the rule.

## Branch 1 — `build/versioned-releases` (first release: `1.0.0`)

- Commit: Add a root `VERSION` file and validate its syntax and one-step bump in PR CI. The initial PR establishes `1.0.0` because the repository has no release tags.
- Commit: After successful tests on `main`, publish the GHCR image as `:<version>`, `:latest`, and `:sha-*`; create an immutable `v<version>` Git tag and GitHub Release with generated notes. Make reruns idempotent and serialize release runs.
- Commit: Update package cleanup to remove only untagged versions, document pinned image deployment, and verify the version, tag, and image refer to the same commit.

## Branch 2 — `feat/reliable-results-and-recovery` (likely minor)

- Commit: Accept a single usable extraction result, including a one-item collection; reject multi-item posts that cannot be shared faithfully instead of sending their first item.
- Commit: Reject truncated TikTok photo posts or missing required slides, and explain the photo-to-video conversion when it is used.
- Commit: Classify known private, unavailable, unsupported, live, timeout, and still-oversize failures into clear messages. Remove Cancel from completed failures and make stale Cancel taps harmless. Keep Cancel only while work can be stopped.
- Commit: Offer **Retry** for temporary network failures and **Retry clip** plus **Send full video** for applicable clip failures. Do not show retry controls for permanent failures. Keep retry context bounded and expiring, enforce the initiating user's access, and handle stale or restarted callbacks with a clear “send the link again” response.

## Branch 3 — `feat/telegram-guidance-and-progress` (likely minor)

- Commit: Give inline video, clip, and full-video choices short action titles and useful secondary lines: platform, media type, clip range or already-known duration, and cached readiness. Keep inline answers fast without remote metadata requests. Use static public thumbnails/icons only if suitable assets exist; otherwise use text and symbols.
- Commit: Shorten `/start` and add a **Share media** button that opens inline use in a chosen chat. Move clipping and direct-chat instructions to `/help`; use matching wording in `/start` and the BotFather inline placeholder recommendation.
- Commit: Show truthful preparing, downloading, optimizing, and Telegram-sending stages with consistent wording in inline and direct-chat flows. Never invent percentages.

## Branch 4 — `feat/adaptive-media-delivery` (likely minor)

- Commit: Select the best available video version fitting Telegram's configured size limit, stepping down from the chosen quality and then trying bounded `ffmpeg` optimization. Apply this to clips and full videos, including direct URL upload checks. Show “Optimizing for Telegram” only during actual optimization. Report oversize only when no acceptable result can be produced; provide no separate “try smaller” menu.
- Commit: Add **Send audio** alongside **Send video** when the source type can provide audio. Verify the stream after selection and send native Telegram audio, including clips where available. Explain unavailable audio rather than silently sending video.
- Commit: Separate cached results by video/audio, clip range, and video quality policy; preserve legacy lookup for default best-quality video where safe.

## Branch 5 — `feat/personal-sharing-settings` (likely minor)

- Commit: Persist settings per Telegram user ID and add `/settings` controls for Video quality (Best that fits, 720p, 480p), Caption (Media title, Original link, None), and Default format (Video, Audio).
- Commit: Treat 720p/480p as progressive-scan quality choices, not literal height caps. Use the format preference to order both inline choices and select direct-chat behavior. Keep explicit per-send captions as overrides; use the existing global `CAPTION_MODE` only as the fallback for users without a saved choice.
- Commit: Apply caption settings when sending fresh and cached media, keeping final media clean and cache entries independent of caption choice.

## Branch 6 — `docs/product-presentation` (likely patch)

- Commit: Upgrade `python-telegram-bot` to a version supporting restrained button styles. Emphasize primary actions and reserve danger styling for active cancellation; older Telegram clients may show ordinary buttons.
- Commit: Lead the README with the product promise and a **text** demonstration placeholder (“paste a link → choose a result → native media appears”), then capabilities, supported media types, setup, and configuration. Do not create a GIF or image for the README.
- Commit: Provide suggested BotFather display name, About line, description, inline placeholder, and small-size avatar concept as **owner actions**. Keep their language consistent with `/start`; do not attempt to change the bot profile from code.

## Branch 7 — `chore/logging-review` (likely patch)

- Commit: Review real send-path logs after the feature branches. Add low-noise operation ID, route, format, stage, elapsed time, cache outcome, and failure category where useful.
- Commit: Verify token, URL-query, and caption redaction; avoid logging media content or repeated stack traces for expected failures.

## Acceptance for each PR

Each PR includes focused tests for its behavior and passes the existing unit suite, Ruff, and strict mypy. Before merging, verify the PR's `VERSION` bump matches compatibility impact. After merging, verify its GHCR tag, Git tag, and GitHub Release. The bot owner applies BotFather profile changes separately. A Mini App and multi-item sharing workflow remain outside this plan.
