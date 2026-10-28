"""Proxy configuration and Telegram/application lifecycle integration."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.request import HTTPXRequest

from telegram_share_bot.app import _post_shutdown, build_application
from telegram_share_bot.config import Settings, load_settings
from telegram_share_bot.config.proxy import load_proxy_options, parse_dns_url, parse_proxy_url
from telegram_share_bot.healthcheck import check_connectivity
from telegram_share_bot.logging_filters import _redact_telegram_secrets
from telegram_share_bot.media.transcode import _ffmpeg_proxy_environment
from telegram_share_bot.proxy.runtime import ProxyRuntime


class TestProxyConfiguration(unittest.TestCase):
    def test_optional_proxy_and_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            options = load_proxy_options(Path("unused.env"))
        self.assertIsNone(options.url)
        self.assertTrue(options.dns_protection)
        self.assertEqual(options.dns_url, "https://cloudflare-dns.com/dns-query")
        for value in (None, "", "   "):
            self.assertIsNone(parse_proxy_url(value))

    def test_authenticated_http_and_https_proxy(self) -> None:
        for url in ("http://user:p%40ss@localhost:3128", "https://[::1]:4443"):
            self.assertEqual(parse_proxy_url(url), url)

    def test_invalid_proxy_errors_do_not_expose_credentials(self) -> None:
        for url in (
            "socks5://user:secret@host:9000",
            "http://user:secret@host:0",
            "http://user:secret@host:99999",
            "http://user:secret@host/path",
            "http://user:secret@host?query",
            "http://user:secret@host#fragment",
            "http://user:sec%0aret@host",
            "http://user:secret@bad host",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError) as raised:
                parse_proxy_url(url)
            self.assertNotIn("secret", str(raised.exception))
            self.assertNotIn("user", str(raised.exception))

    def test_dns_provider_requires_compatible_https_url(self) -> None:
        self.assertEqual(
            parse_dns_url("https://dns.example/resolve"), "https://dns.example/resolve"
        )
        for url in (
            "http://dns.example/resolve",
            "https://user:secret@dns.example/resolve",
            "https://dns.example/resolve?token=secret",
            "https://dns.example:0/resolve",
        ):
            with self.assertRaises(RuntimeError):
                parse_dns_url(url)

    def test_load_settings_proxy_fields_and_repr(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BOT_TOKEN": "valid:token",
                "STORAGE_CHAT_ID": "42",
                "ALLOW_PUBLIC": "true",
                "HTTP_PROXY": "http://user:secret@proxy.example:3128",
                "PROXY_DNS_PROTECTION": "false",
                "PROXY_DNS_URL": "https://dns.example/resolve",
            },
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.http_proxy, "http://user:secret@proxy.example:3128")
        self.assertFalse(settings.proxy_dns_protection)
        self.assertEqual(settings.proxy_dns_url, "https://dns.example/resolve")
        self.assertNotIn("secret", repr(settings))

    def test_logs_redact_proxy_urls_and_auth_headers(self) -> None:
        for text in (
            "proxy http://user:secret@proxy.example:3128",
            "Proxy-Authorization: Basic c2VjcmV0",
            "Authorization=Bearer private-token",
        ):
            result = _redact_telegram_secrets(text)
            self.assertNotIn("secret", result)
            self.assertNotIn("c2VjcmV0", result)
            self.assertNotIn("private-token", result)

    def test_ffmpeg_environment_cannot_bypass_or_replace_proxy(self) -> None:
        runtime = MagicMock(spec=ProxyRuntime)
        runtime.client_proxy_url = "http://proxy.example:3128"
        with (
            patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://other.invalid:9000",
                    "HTTPS_PROXY": "http://other.invalid",
                    "ALL_PROXY": "http://other.invalid",
                    "NO_PROXY": "*",
                    "no_proxy": "*",
                    "PATH": "keep-path",
                },
                clear=True,
            ),
            patch("telegram_share_bot.media.transcode.get_proxy_runtime", return_value=runtime),
        ):
            environment = _ffmpeg_proxy_environment()
        self.assertEqual(
            environment,
            {
                "http_proxy": runtime.client_proxy_url,
                "https_proxy": runtime.client_proxy_url,
                "PATH": "keep-path",
            },
        )
        with patch("telegram_share_bot.media.transcode.get_proxy_runtime", return_value=None):
            self.assertIsNone(_ffmpeg_proxy_environment())


class TestProxyApplication(unittest.TestCase):
    def _settings(self, directory: Path) -> Settings:
        return Settings(
            "valid:token",
            42,
            1024,
            10,
            directory,
            directory / "cache.db",
            False,
            http_proxy="http://proxy.example:3128",
            proxy_dns_protection=False,
        )

    def test_polling_and_upload_transport_use_the_same_explicit_proxy(self) -> None:
        runtime = MagicMock(spec=ProxyRuntime)
        runtime.client_proxy_url = "http://proxy.example:3128"
        runtime.dns_protection = False
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch(
                    "telegram_share_bot.app.load_settings",
                    return_value=self._settings(Path(temporary)),
                ),
                patch("telegram_share_bot.app.configure_proxy", return_value=runtime),
                patch("telegram_share_bot.app.HTTPXRequest", wraps=HTTPXRequest) as request,
            ):
                application = build_application()
            self.assertEqual(request.call_count, 2)
            for call in request.call_args_list:
                self.assertEqual(call.kwargs["proxy"], runtime.client_proxy_url)
                self.assertFalse(call.kwargs["httpx_kwargs"]["trust_env"])
            self.assertEqual(application.bot.request._media_write_timeout, 180)

    def test_build_failure_closes_gateway(self) -> None:
        runtime = MagicMock(spec=ProxyRuntime)
        runtime.client_proxy_url = "http://proxy.example:3128"
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch(
                    "telegram_share_bot.app.load_settings",
                    return_value=self._settings(Path(temporary)),
                ),
                patch("telegram_share_bot.app.configure_proxy", return_value=runtime),
                patch(
                    "telegram_share_bot.app.Application.builder",
                    side_effect=RuntimeError("build failed"),
                ),
                patch("telegram_share_bot.app.close_proxy") as close,
            ):
                with self.assertRaisesRegex(RuntimeError, "build failed"):
                    build_application()
            close.assert_called_once_with(runtime)

    def test_shutdown_closes_gateway_even_if_other_cleanup_fails(self) -> None:
        runtime = MagicMock(spec=ProxyRuntime)
        application = MagicMock()
        application.bot_data = {"proxy_runtime": runtime}
        with (
            patch(
                "telegram_share_bot.app._shutdown_resources",
                new_callable=AsyncMock,
                side_effect=RuntimeError("shutdown failed"),
            ),
            patch("telegram_share_bot.app.close_proxy") as close,
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(_post_shutdown(application))
        close.assert_called_once_with(runtime)

    def test_healthcheck_uses_proxy_without_environment_bypass(self) -> None:
        runtime = MagicMock(spec=ProxyRuntime)
        runtime.client_proxy_url = "http://proxy.example:3128"
        with (
            patch.dict("os.environ", {"HTTP_PROXY": runtime.client_proxy_url}, clear=True),
            patch("telegram_share_bot.healthcheck.configure_proxy", return_value=runtime),
            patch("telegram_share_bot.healthcheck.httpx.Client") as client,
            patch("telegram_share_bot.healthcheck.close_proxy") as close,
        ):
            client.return_value.__enter__.return_value.get.return_value.status_code = 200
            check_connectivity()
        self.assertEqual(client.call_args.kwargs["proxy"], runtime.client_proxy_url)
        self.assertFalse(client.call_args.kwargs["trust_env"])
        close.assert_called_once_with(runtime)
