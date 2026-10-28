"""Validate the optional outbound proxy configuration without exposing credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from telegram_share_bot.config.models import DEFAULT_PROXY_DNS_URL
from telegram_share_bot.config.parsing import parse_bool


@dataclass(frozen=True, slots=True)
class ProxyOptions:
    url: str | None = field(repr=False)
    dns_protection: bool = True
    dns_url: str = DEFAULT_PROXY_DNS_URL


def parse_proxy_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    error = "HTTP_PROXY must be an http:// or https:// proxy URL with a valid host and port."
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.port == 0
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or any(
                ord(char) < 32
                for char in unquote(parsed.username or "") + unquote(parsed.password or "")
            )
        ):
            raise ValueError
        parsed.hostname.encode("idna")
    except (ValueError, UnicodeError):
        raise RuntimeError(error) from None
    return value.rstrip("/")


def parse_dns_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port == 0
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
    except ValueError:
        raise RuntimeError("PROXY_DNS_URL must be an HTTPS DNS-JSON endpoint URL.") from None
    return value


def load_proxy_options(env_file: Path) -> ProxyOptions:
    return ProxyOptions(
        parse_proxy_url(os.getenv("HTTP_PROXY")),
        parse_bool(
            "PROXY_DNS_PROTECTION",
            os.getenv("PROXY_DNS_PROTECTION"),
            default=True,
            env_file=env_file,
        ),
        parse_dns_url(os.getenv("PROXY_DNS_URL") or DEFAULT_PROXY_DNS_URL),
    )
