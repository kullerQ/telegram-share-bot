"""Client transports route origin requests through the configured proxy."""

from __future__ import annotations

import asyncio
import http.server
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import certifi
import httpx
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.networking.impersonate import ImpersonateTarget

from telegram_share_bot.media.clips import _download_hls_clip_stream
from telegram_share_bot.media.models import TimeRange
from telegram_share_bot.media.network import (
    GuardedAsyncHTTPTransport,
    GuardedYoutubeDL,
    _PinnedCurlSession,
    create_youtube_dl,
)
from telegram_share_bot.media.security import _safe_dns_resolution, is_safe_media_url


class _ProxyRecorder:
    def __init__(self) -> None:
        self.targets: list[str] = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                recorder.targets.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> _ProxyRecorder:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _TlsOrigin:
    def __init__(self, certfile: Path, keyfile: Path) -> None:
        self.servernames: list[str | None] = []
        origin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        context.set_servername_callback(lambda _ssl, name, _ctx: origin.servernames.append(name))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def __enter__(self) -> _TlsOrigin:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _TlsProxyRecorder(_ProxyRecorder):
    def __init__(self, origin_port: int) -> None:
        self.targets: list[str] = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self) -> None:
                recorder.targets.append(self.path)
                upstream = socket.create_connection(("127.0.0.1", origin_port), timeout=3)
                self.connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                sockets = (self.connection, upstream)
                try:
                    while True:
                        readable, _, _ = select.select(sockets, (), (), 3)
                        if not readable:
                            return
                        for source in readable:
                            chunk = source.recv(65536)
                            if not chunk:
                                return
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(chunk)
                finally:
                    upstream.close()

            def log_message(self, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)


class TestProxyAwareYtDlp(unittest.TestCase):
    def test_curl_session_uses_runtime_proxy_without_origin_resolution(self) -> None:
        proxy_url = "http://proxy.example:8080"
        runtime = SimpleNamespace(client_proxy_url=proxy_url, dns_protection=False)
        response = SimpleNamespace(status_code=200, headers={}, close=lambda: None)
        with (
            patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime),
            patch(
                "telegram_share_bot.media.network.resolve_safe_addresses",
                side_effect=AssertionError("origin resolution must remain remote"),
            ),
            patch(
                "telegram_share_bot.media.network.Session.request",
                return_value=response,
            ) as send,
            _PinnedCurlSession(trust_env=False) as session,
        ):
            result = session.request("GET", "https://origin.invalid/video")
        self.assertIs(result, response)
        self.assertEqual(send.call_args.kwargs["proxy"], proxy_url)
        self.assertEqual(send.call_args.kwargs["proxies"], {})

    def test_curl_connects_only_to_fake_proxy_and_preserves_origin_target(self) -> None:
        with _ProxyRecorder() as proxy:
            runtime = SimpleNamespace(client_proxy_url=proxy.url, dns_protection=False)
            for bypass_var in ("NO_PROXY", "no_proxy"):
                with (
                    patch.dict(os.environ, {bypass_var: "*"}),
                    patch(
                        "telegram_share_bot.media.network.get_proxy_runtime",
                        return_value=runtime,
                    ),
                ):
                    with create_youtube_dl({"quiet": True}) as ydl:
                        response = cast(Any, ydl).urlopen(
                            Request(
                                "http://origin.invalid/video",
                                extensions={"impersonate": ImpersonateTarget("chrome")},
                            )
                        )
                        with response:
                            self.assertEqual(response.read(), b"ok")
            self.assertEqual(proxy.targets, ["http://origin.invalid/video"] * 2)

    def test_proxy_redirect_rejects_private_literal_before_second_hop(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://proxy.example:8080", dns_protection=False
        )
        redirect = SimpleNamespace(
            status_code=302,
            headers={"location": "https://127.0.0.1/metadata"},
            close=lambda: None,
        )
        with (
            patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime),
            patch(
                "telegram_share_bot.media.network.Session.request",
                return_value=redirect,
            ) as send,
            _PinnedCurlSession(trust_env=False) as session,
        ):
            with self.assertRaisesRegex(RequestError, "unsafe destination hostname"):
                session.request("GET", "https://origin.invalid/video")
        send.assert_called_once()

    def test_yt_dlp_configures_every_handler_with_central_proxy(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://proxy.example:8080", dns_protection=False
        )
        with patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime):
            with create_youtube_dl({"quiet": True}) as ydl:
                self.assertEqual(ydl.params["proxy"], runtime.client_proxy_url)
                director = cast(Any, ydl)._request_director
                self.assertTrue(director.handlers)
                for handler in director.handlers.values():
                    self.assertEqual(handler.proxies.get("all"), runtime.client_proxy_url)

    def test_external_ffmpeg_inputs_force_proxy_tls_and_protocol_options(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://proxy.example:8080", dns_protection=False
        )
        options = {
            "quiet": True,
            "external_downloader_args": {
                "ffmpeg_i1": [
                    "-http_proxy",
                    "http://other-proxy.invalid:8888",
                    "-headers",
                    "X-Test: keep",
                ],
                "ffmpeg_i": ["-tls_verify", "0"],
                "ffmpeg_o": ["-movflags", "+faststart"],
            },
        }
        with patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime):
            with create_youtube_dl(options) as ydl:
                configured = cast(Any, ydl.params["external_downloader_args"])
        forced = [
            "-http_proxy",
            runtime.client_proxy_url,
            "-tls_verify",
            "1",
            "-ca_file",
            certifi.where(),
            "-protocol_whitelist",
            "http,https,tcp,tls,crypto,data",
        ]
        self.assertEqual(configured["ffmpeg_i1"], ["-headers", "X-Test: keep", *forced])
        self.assertEqual(configured["ffmpeg_i"], forced)
        self.assertEqual(configured["ffmpeg_o"], ["-movflags", "+faststart"])

    def test_direct_youtube_dl_constructor_cannot_skip_runtime_proxy(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://proxy.example:8080", dns_protection=False
        )
        with patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime):
            with GuardedYoutubeDL() as ydl:
                self.assertEqual(ydl.params["proxy"], runtime.client_proxy_url)

    def test_caller_proxy_bypass_is_rejected(self) -> None:
        runtime = SimpleNamespace(client_proxy_url="http://127.0.0.1:8123", dns_protection=False)
        with patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime):
            with self.assertRaisesRegex(RuntimeError, "Caller proxy settings"):
                create_youtube_dl({"quiet": True, "proxy": ""})
            with create_youtube_dl({"quiet": True}) as ydl:
                with self.assertRaisesRegex(RequestError, "bypass"):
                    cast(Any, ydl).urlopen(
                        Request("https://origin.invalid/video", proxies={"no": "*"})
                    )
                with self.assertRaisesRegex(RequestError, "Caller proxy settings"):
                    cast(Any, ydl).urlopen(
                        Request("https://origin.invalid/video", proxies={"all": None})
                    )


class TestProxyAwarePreviewTransport(unittest.TestCase):
    def test_httpx_transport_uses_proxy_without_local_origin_backend(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://127.0.0.1:8123", dns_protection=False
        )
        with patch("telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime):
            transport = GuardedAsyncHTTPTransport()
        pool = cast(Any, transport._pool)
        self.assertEqual(type(pool).__name__, "AsyncHTTPProxy")
        self.assertEqual(pool._proxy_url.host, b"127.0.0.1")
        self.assertEqual(pool._proxy_url.port, 8123)
        self.assertNotEqual(type(pool._network_backend).__name__, "_GuardedAsyncBackend")

    def test_httpx_sends_request_to_fake_proxy(self) -> None:
        with _ProxyRecorder() as proxy:
            runtime = SimpleNamespace(client_proxy_url=proxy.url, dns_protection=False)

            async def request_preview() -> bytes:
                with patch(
                    "telegram_share_bot.media.network.get_proxy_runtime", return_value=runtime
                ):
                    async with httpx.AsyncClient(
                        transport=GuardedAsyncHTTPTransport(), timeout=2
                    ) as client:
                        response = await client.get("http://origin.invalid/preview")
                        return response.content

            self.assertEqual(asyncio.run(request_preview()), b"ok")
            self.assertEqual(proxy.targets, ["http://origin.invalid/preview"])


class TestProxyTlsIdentity(unittest.TestCase):
    def test_clients_tunnel_tls_with_origin_sni_and_certificate_validation(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            candidate = Path(program_files) / "Git" / "usr" / "bin" / "openssl.exe"
            if candidate.is_file():
                openssl = str(candidate)
        if openssl is None:
            self.skipTest("OpenSSL is required to create the local TLS fixture")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            certfile, keyfile = temp / "origin.crt", temp / "origin.key"
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-keyout",
                    str(keyfile),
                    "-out",
                    str(certfile),
                    "-subj",
                    "/CN=origin.invalid",
                    "-addext",
                    "subjectAltName=DNS:origin.invalid",
                ],
                check=True,
                capture_output=True,
            )
            with _TlsOrigin(certfile, keyfile) as origin:
                proxy = _TlsProxyRecorder(origin.port)
                proxy.__enter__()
                runtime = SimpleNamespace(
                    client_proxy_url=f"http://127.0.0.1:{proxy.server.server_port}",
                    dns_protection=False,
                )
                try:
                    url = f"https://origin.invalid:{origin.port}/secure"
                    with (
                        patch(
                            "telegram_share_bot.media.network.get_proxy_runtime",
                            return_value=runtime,
                        ),
                        _PinnedCurlSession(trust_env=False) as curl,
                    ):
                        response = curl.request("GET", url, verify=str(certfile))
                        self.assertEqual(b"".join(response.iter_content()), b"ok")
                        response.close()

                    async def request_httpx() -> bytes:
                        with patch(
                            "telegram_share_bot.media.network.get_proxy_runtime",
                            return_value=runtime,
                        ):
                            transport = GuardedAsyncHTTPTransport()
                        transport._pool._ssl_context = ssl.create_default_context(
                            cafile=str(certfile)
                        )
                        async with httpx.AsyncClient(transport=transport, timeout=3) as client:
                            response = await client.get(url)
                            return response.content

                    self.assertEqual(asyncio.run(request_httpx()), b"ok")
                    self.assertEqual(
                        proxy.targets,
                        [f"origin.invalid:{origin.port}"] * 2,
                    )
                    self.assertEqual(origin.servernames, ["origin.invalid", "origin.invalid"])
                finally:
                    proxy.__exit__()


class TestProxyAwareSecurity(unittest.TestCase):
    def test_unprotected_runtime_checks_syntax_without_local_dns(self) -> None:
        runtime = SimpleNamespace(dns_protection=False)
        with (
            patch("telegram_share_bot.media.security._proxy_runtime", return_value=runtime),
            patch(
                "telegram_share_bot.media.security._REAL_GETADDRINFO",
                side_effect=AssertionError("local DNS must not run"),
            ),
        ):
            self.assertTrue(is_safe_media_url("https://media.example/video"))
            self.assertTrue(is_safe_media_url("https://example.com/video"))
            self.assertTrue(is_safe_media_url("https://8.8.8.8/video"))
            self.assertFalse(is_safe_media_url("https://127.0.0.1/video"))
            for alternate_private_ip in (
                "127.1",
                "0177.0.0.1",
                "0x7f.1",
                "192.168.1",
            ):
                self.assertFalse(
                    is_safe_media_url(f"https://{alternate_private_ip}/video"),
                    alternate_private_ip,
                )
            with _safe_dns_resolution():
                pass

    def test_protected_runtime_resolves_through_runtime(self) -> None:
        runtime = SimpleNamespace(
            dns_protection=True,
            resolve_addresses=lambda host: (IPv4Address("93.184.216.34"),),
        )
        with (
            patch("telegram_share_bot.media.security._proxy_runtime", return_value=runtime),
            patch(
                "telegram_share_bot.media.security._REAL_GETADDRINFO",
                side_effect=AssertionError("local DNS must not run"),
            ),
        ):
            self.assertTrue(is_safe_media_url("https://media.example/video"))


class TestProxyAwareHls(unittest.TestCase):
    def test_ffmpeg_input_routes_playlist_and_nested_requests_through_proxy(self) -> None:
        runtime = SimpleNamespace(
            client_proxy_url="http://user:secret@proxy.example:8080",
            dns_protection=False,
        )
        with (
            patch("telegram_share_bot.media.clips.get_proxy_runtime", return_value=runtime),
            patch("telegram_share_bot.media.clips.is_safe_media_url", return_value=True),
            patch("telegram_share_bot.media.clips._run_bounded_clip_ffmpeg") as run_ffmpeg,
        ):
            _download_hls_clip_stream(
                {"url": "https://media.example/master.m3u8"},
                ffmpeg_bin="ffmpeg",
                time_range=TimeRange(0, 10),
                media_type="video",
                output=Path("clip.mp4"),
                deadline=10,
                abort_event=None,
                source_limit=1024,
                timeout_seconds=10,
            )
        argv = run_ffmpeg.call_args.args[0]
        self.assertEqual(argv[argv.index("-http_proxy") + 1], runtime.client_proxy_url)
        self.assertEqual(argv[argv.index("-tls_verify") + 1], "1")
        self.assertEqual(argv[argv.index("-ca_file") + 1], certifi.where())
        self.assertEqual(
            argv[argv.index("-protocol_whitelist") + 1],
            "http,https,tcp,tls,crypto,data",
        )


if __name__ == "__main__":
    unittest.main()
