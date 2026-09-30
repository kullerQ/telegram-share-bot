"""Native transport tests for DNS pinning and redirect boundaries."""

from __future__ import annotations

import http.server
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from ipaddress import IPv4Address
from typing import Any
from unittest.mock import patch

import httpx
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.networking.impersonate import ImpersonateTarget

from telegram_share_bot.media.network import (
    GuardedAsyncHTTPTransport,
    GuardedCurlCFFIRH,
    _PinnedCurlSession,
    create_youtube_dl,
)
from telegram_share_bot.media.security import _safe_dns_resolution


class _Server(AbstractContextManager["_Server"]):
    def __init__(self, callback: Any) -> None:
        self.requests: list[
            tuple[str, str | None, str | None, str | None, str | None]
        ] = []
        server_state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                authorization = self.headers.get("Authorization")
                server_state.requests.append(
                    (
                        self.path,
                        self.headers.get("Host"),
                        authorization,
                        self.headers.get("Cookie"),
                        self.headers.get("Referer"),
                    )
                )
                status, headers, body = callback(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _impersonated_request(url: str, headers: dict[str, str] | None = None) -> Request:
    return Request(
        url,
        headers=headers,
        extensions={"impersonate": ImpersonateTarget("chrome")},
    )


class TestGuardedCurlTransport(unittest.TestCase):
    def test_invalid_destination_authority_is_rejected_before_resolution(self) -> None:
        with patch("telegram_share_bot.media.network.resolve_safe_addresses") as resolver:
            with _PinnedCurlSession(trust_env=False) as session:
                with self.assertRaises(RequestError):
                    session.request("GET", "http://origin.test:0/video")
        resolver.assert_not_called()

    def test_request_uses_pinned_ip_and_preserves_url_host(self) -> None:
        with _Server(lambda _path: (200, {}, b"native curl")) as server:
            url = f"http://origin.test:{server.port}/media"
            with patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                return_value=(IPv4Address("127.0.0.1"),),
            ) as resolver:
                with create_youtube_dl({"quiet": True}) as ydl:
                    handler = ydl._request_director.handlers["CurlCFFI"]
                    self.assertIsInstance(handler, GuardedCurlCFFIRH)
                    response = ydl.urlopen(_impersonated_request(url))
                    with response:
                        self.assertEqual(response.read(), b"native curl")
            self.assertEqual(resolver.call_args_list[0].args, ("origin.test",))
            self.assertEqual(
                server.requests,
                [("/media", f"origin.test:{server.port}", None, None, None)],
            )

    def test_requests_handler_uses_the_thread_local_dns_guard(self) -> None:
        with _Server(lambda _path: (200, {}, b"requests")) as server:
            url = f"http://requests.test:{server.port}/media"

            def getaddrinfo(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
                return [(2, 1, 6, "", ("127.0.0.1", server.port))]

            with (
                patch(
                    "telegram_share_bot.media.security._REAL_GETADDRINFO",
                    side_effect=getaddrinfo,
                ),
                patch("telegram_share_bot.media.security._is_safe_ip", return_value=True),
                _safe_dns_resolution(),
            ):
                with create_youtube_dl({"quiet": True}) as ydl:
                    response = ydl.urlopen(Request(url))
                    with response:
                        self.assertEqual(response.read(), b"requests")
            self.assertEqual(
                server.requests,
                [("/media", f"requests.test:{server.port}", None, None, None)],
            )

    def test_concurrent_stream_requests_keep_their_own_dns_pin(self) -> None:
        with _Server(lambda path: (200, {}, path.encode())) as server:
            with patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                return_value=(IPv4Address("127.0.0.1"),),
            ):
                with _PinnedCurlSession(trust_env=False) as session:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [
                            pool.submit(
                                session.request,
                                "GET",
                                f"http://{host}.test:{server.port}/{host}",
                            )
                            for host in ("first", "second")
                        ]
                        responses = [future.result(timeout=3) for future in futures]
                    try:
                        self.assertEqual(
                            [b"".join(response.iter_content()) for response in responses],
                            [b"/first", b"/second"],
                        )
                    finally:
                        for response in responses:
                            response.close()
            self.assertEqual(
                [request[1] for request in server.requests],
                [f"first.test:{server.port}", f"second.test:{server.port}"],
            )

    def test_cross_origin_redirect_drops_explicit_cookie_parameter(self) -> None:
        with _Server(
            lambda path: (
                (302, {"Location": f"http://cdn.test:{server.port}/asset"}, b"")
                if path == "/start"
                else (200, {}, b"asset")
            )
        ) as server:
            with patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                return_value=(IPv4Address("127.0.0.1"),),
            ):
                with _PinnedCurlSession(trust_env=False) as session:
                    response = session.request(
                        "GET",
                        f"http://origin.test:{server.port}/start",
                        headers={"Referer": "https://private.example/"},
                        cookies={"private": "secret"},
                    )
                    try:
                        self.assertEqual(b"".join(response.iter_content()), b"asset")
                    finally:
                        response.close()

            self.assertIsNotNone(server.requests[0][3])
            self.assertIsNone(server.requests[1][3])
            self.assertIsNone(server.requests[1][4])

    def test_redirect_is_validated_and_private_hop_is_not_requested(self) -> None:
        with _Server(
            lambda _path: (
                302,
                {"Location": f"http://internal.test:{server.port}/metadata"},
                b"",
            )
        ) as server:
            def resolve(host: str) -> tuple[IPv4Address, ...]:
                if host == "internal.test":
                    raise ValueError("private target")
                return (IPv4Address("127.0.0.1"),)

            with patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                side_effect=resolve,
            ) as resolver:
                with create_youtube_dl({"quiet": True}) as ydl:
                    with self.assertRaises(RequestError):
                        ydl.urlopen(_impersonated_request(f"http://origin.test:{server.port}/start"))
            self.assertEqual([call.args[0] for call in resolver.call_args_list], [
                "origin.test",
                "internal.test",
            ])
            self.assertEqual(len(server.requests), 1)

    def test_cross_origin_redirect_does_not_forward_authorization(self) -> None:
        with _Server(
            lambda path: (
                (302, {"Location": f"http://cdn.test:{server.port}/asset"}, b"")
                if path == "/start"
                else (200, {}, b"asset")
            )
        ) as server:
            with patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                return_value=(IPv4Address("127.0.0.1"),),
            ):
                with create_youtube_dl({"quiet": True}) as ydl:
                    response = ydl.urlopen(
                        _impersonated_request(
                            f"http://origin.test:{server.port}/start",
                            {
                                "Authorization": "Bearer private",
                                "Cookie": "header-secret=value",
                                "Referer": "https://private.example/",
                            },
                        )
                    )
                    with response:
                        self.assertEqual(response.read(), b"asset")
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.requests[0][2], "Bearer private")
            self.assertIsNone(server.requests[1][2])
            self.assertEqual(server.requests[1][1], f"cdn.test:{server.port}")
            self.assertIsNone(server.requests[1][3])
            self.assertIsNone(server.requests[1][4])

    def test_configured_proxy_is_rejected(self) -> None:
        with create_youtube_dl(
            {"quiet": True, "proxy": "http://127.0.0.1:8123"}
        ) as ydl:
            with self.assertRaisesRegex(RuntimeError, "proxy settings are unsupported"):
                ydl.urlopen(
                    _impersonated_request("https://example.com/video")
                )


class TestGuardedAsyncTransport(unittest.IsolatedAsyncioTestCase):
    async def test_httpx_connects_to_pinned_address_and_keeps_host_header(self) -> None:
        with _Server(lambda _path: (200, {}, b"preview")) as server:
            url = f"http://preview.test:{server.port}/page"
            with patch(
                "telegram_share_bot.media.network._checked_addresses",
                return_value=(IPv4Address("127.0.0.1"),),
            ):
                async with httpx.AsyncClient(
                    transport=GuardedAsyncHTTPTransport(),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.get(url)
            self.assertEqual(response.content, b"preview")
            self.assertEqual(
                server.requests,
                [("/page", f"preview.test:{server.port}", None, None, None)],
            )

    async def test_httpx_fails_closed_when_dns_contains_private_address(self) -> None:
        with patch(
            "telegram_share_bot.media.network.resolve_safe_addresses",
            side_effect=ValueError("private target"),
        ):
            async with httpx.AsyncClient(
                transport=GuardedAsyncHTTPTransport(),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with self.assertRaises(httpx.ConnectError):
                    await client.get("http://private.test/page")


if __name__ == "__main__":
    unittest.main()
