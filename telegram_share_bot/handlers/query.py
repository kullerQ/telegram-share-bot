"""Telegram command and inline-query handlers."""

from __future__ import annotations

import logging
from uuid import uuid4

from telegram import (
    InlineQueryResultArticle,
    Update,
)
from telegram.ext import ContextTypes

from telegram_share_bot import strings
from telegram_share_bot.media.models import (
    MediaFormat,
    TimeRange,
    VideoQualityPolicy,
)
from telegram_share_bot.media.requests import (
    extract_media_request,
)
from telegram_share_bot.media.security import (
    is_allowed_media_host,
    is_https_url,
)
from telegram_share_bot.platforms import previews as platform_previews
from telegram_share_bot.platforms.urls import has_url_credentials

from .delivery import _error_article, _pending_media_article
from .preferences import _user_preferences
from .state import (
    _answer_inline_query,
    _cache,
    _cache_quality_policy,
    _is_user_allowed,
    _settings,
    _store_pending_url,
)

logger = logging.getLogger("telegram_share_bot.handlers")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer quickly; heavy download happens after the result is chosen."""
    query = update.inline_query
    if query is None:
        return

    if not _is_user_allowed(context, query.from_user.id if query.from_user else None):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_ACCESS_DENIED_TITLE,
                    strings.INLINE_ACCESS_DENIED_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    text = (query.query or "").strip()
    if not text:
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_EMPTY_TITLE,
                    strings.INLINE_EMPTY_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    request = extract_media_request(text)
    url = request.url
    custom_caption = request.custom_caption
    time_range = request.time_range
    if url is None:
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.INLINE_NO_URL_DESCRIPTION,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    # Reject credentials and malformed authorities before preview lookup or
    # storing the query in process-local pending state.
    if has_url_credentials(url):
        await _answer_inline_query(
            query,
            results=[
                _error_article(strings.INLINE_NO_URL_TITLE, strings.DOWNLOAD_UNSAFE_URL)
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    settings = _settings(context)
    if not is_allowed_media_host(url, settings.allowed_media_hosts):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.DOWNLOAD_HOST_NOT_ALLOWED,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    if settings.https_only and not is_https_url(url):
        await _answer_inline_query(
            query,
            results=[
                _error_article(
                    strings.INLINE_NO_URL_TITLE,
                    strings.DOWNLOAD_HTTPS_REQUIRED,
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    user_id = query.from_user.id if query.from_user else None
    preferences = await _user_preferences(context, user_id)

    resolver = context.bot_data.get("preview_resolver")
    preview = await platform_previews.resolve_preview(
        url,
        resolver if isinstance(resolver, platform_previews.PreviewResolver) else None,
    )

    choices: list[tuple[str, MediaFormat, TimeRange | None, bool]] = []
    choice_token = uuid4().hex
    preferred_format = preferences.default_format or MediaFormat.VIDEO
    format_order = (
        (preferred_format, MediaFormat.AUDIO)
        if preferred_format is MediaFormat.VIDEO
        else (MediaFormat.AUDIO, MediaFormat.VIDEO)
    )
    if time_range is not None:
        for prefix, selected_range, force_full in (
            ("clip", time_range, False),
            ("full", None, True),
        ):
            choices.extend(
                (
                    f"{prefix}-{media_format.value}:{choice_token}",
                    media_format,
                    selected_range,
                    force_full,
                )
                for media_format in format_order
            )
    else:
        choices = [
            (f"{media_format.value}:{choice_token}", media_format, None, False)
            for media_format in format_order
        ]

    results: list[InlineQueryResultArticle] = []
    for result_id, media_format, selected_range, force_full_title in choices:
        quality_policy = (
            preferences.video_quality
            if media_format is MediaFormat.VIDEO
            else VideoQualityPolicy.AUTO
        )
        _store_pending_url(
            context,
            result_id,
            url,
            custom_caption=custom_caption,
            time_range=selected_range,
            media_format=media_format,
            quality_policy=quality_policy,
            preferences=preferences,
            owner_user_id=user_id,
        )
        cache_quality = _cache_quality_policy(media_format, quality_policy)
        cached = (
            await _cache(context).get_preferred_video(
                url, time_range=selected_range, quality_policy=cache_quality
            )
            if media_format is MediaFormat.VIDEO
            else await _cache(context).get(
                url,
                time_range=selected_range,
                media_format=media_format,
                quality_policy=cache_quality,
            )
        )
        results.append(
            _pending_media_article(
                result_id,
                url,
                logo_base_url=settings.platform_logo_base_url,
                preview=preview,
                cached=cached,
                time_range=selected_range,
                media_format=media_format,
                quality_policy=quality_policy,
                force_full_title=force_full_title,
            )
        )
    await _answer_inline_query(
        query,
        results=results,
        cache_time=1,
        is_personal=True,
    )
