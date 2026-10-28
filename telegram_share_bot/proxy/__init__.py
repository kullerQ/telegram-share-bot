"""Optional HTTP proxy routing with protected destination DNS resolution."""

from telegram_share_bot.proxy.resolver import DEFAULT_DNS_URL
from telegram_share_bot.proxy.runtime import (
    ProxyRuntime,
    close_proxy,
    configure_proxy,
    get_proxy_runtime,
)

__all__ = [
    "DEFAULT_DNS_URL",
    "ProxyRuntime",
    "close_proxy",
    "configure_proxy",
    "get_proxy_runtime",
]
