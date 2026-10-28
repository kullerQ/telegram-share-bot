"""Exercise real proxy forwarding, upload bodies, and opt-out redirect filtering."""

from __future__ import annotations

import base64
import ipaddress
import select
import socket
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from telegram_share_bot.proxy.gateway import ProxyGateway


class _Origin(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.observed.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1/should-not-connect")
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Proxy(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        request = bytearray()
        self.request.settimeout(3)
        while not request.endswith(b"\r\n\r\n"):
            part = self.request.recv(1)
            if not part:
                return
            request.extend(part)
        self.server.observed.append(bytes(request))
        with socket.create_connection(self.server.origin, timeout=3) as origin:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            peers = {self.request: origin, origin: self.request}
            while True:
                ready, _, _ = select.select(list(peers), [], [], 3)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)


class TestHttpGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
        self.origin.observed = []
        self.proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Proxy)
        self.proxy.daemon_threads = True
        self.proxy.origin = self.origin.server_address
        self.proxy.observed = []
        self.threads = []
        for server in (self.origin, self.proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.proxy_url = f"http://owner:upstream-secret@127.0.0.1:{self.proxy.server_address[1]}"

    def tearDown(self) -> None:
        for server in (self.proxy, self.origin):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def test_large_upload_is_preserved_and_proxy_auth_never_reaches_origin(self) -> None:
        resolver = SimpleNamespace(resolve=lambda host: (ipaddress.ip_address("1.1.1.1"),))
        gateway = ProxyGateway(self.proxy_url, resolver)
        body = b"upload bytes" * 30_000
        try:
            with httpx.Client(proxy=gateway.client_url, trust_env=False, timeout=5) as client:
                response = client.post("http://upload.example.test/resource?value=1", content=body)
            self.assertEqual(response.status_code, 200)
            path, headers, observed = self.origin.observed[0]
            self.assertEqual(path, "/resource?value=1")
            self.assertEqual(observed, body)
            self.assertEqual(headers["Host"], "upload.example.test")
            self.assertNotIn("Proxy-Authorization", headers)
            upstream = self.proxy.observed[0]
            self.assertTrue(upstream.startswith(b"CONNECT 1.1.1.1:80 HTTP/1.1\r\n"))
            self.assertIn(base64.b64encode(b"owner:upstream-secret"), upstream)
        finally:
            gateway.close()

    def test_unprotected_mode_resolves_remotely_and_blocks_private_redirect(self) -> None:
        gateway = ProxyGateway(self.proxy_url, None)
        real_getaddrinfo = socket.getaddrinfo

        def resolve(host: object, *args: object, **kwargs: object) -> object:
            self.assertEqual(host, "127.0.0.1", "origin domain must never use local DNS")
            return real_getaddrinfo(host, *args, **kwargs)

        try:
            with (
                patch("socket.getaddrinfo", side_effect=resolve),
                httpx.Client(
                    proxy=gateway.client_url, trust_env=False, timeout=5, follow_redirects=True
                ) as client,
            ):
                response = client.get("http://public.example.test/redirect")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(len(self.proxy.observed), 1)
            self.assertTrue(
                self.proxy.observed[0].startswith(b"CONNECT public.example.test:80 HTTP/1.1\r\n")
            )
        finally:
            gateway.close()
