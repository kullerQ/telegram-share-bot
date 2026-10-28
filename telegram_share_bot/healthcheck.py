"""Check Telegram connectivity using the same explicit proxy policy as the bot."""

from __future__ import annotations

import sys

import httpx

from telegram_share_bot.config.models import _ENV_PATH
from telegram_share_bot.config.proxy import load_proxy_options
from telegram_share_bot.proxy.runtime import close_proxy, configure_proxy


def check_connectivity() -> None:
    options = load_proxy_options(_ENV_PATH)
    runtime = configure_proxy(options.url, options.dns_protection, options.dns_url)
    try:
        with httpx.Client(
            proxy=runtime.client_proxy_url if runtime else None,
            trust_env=False,
            timeout=5.0,
            follow_redirects=False,
        ) as client:
            response = client.get("https://api.telegram.org")
            if not 200 <= response.status_code < 400:
                raise RuntimeError("Telegram connectivity check failed")
    finally:
        close_proxy(runtime)


def main() -> None:
    try:
        check_connectivity()
    except Exception:
        # A proxy exception can contain credentials or provider diagnostic text.
        print(
            "Telegram connectivity check failed; check the configured network/proxy.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
