from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from telegram_share_bot.proxy.gateway import ProxyGateway, _host_is_forbidden
from telegram_share_bot.proxy.resolver import ProtectedResolver
from telegram_share_bot.proxy.runtime import (
    DEFAULT_DNS_URL,
    ProxyRuntime,
    close_proxy,
    configure_proxy,
    get_proxy_runtime,
)

_TEST_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIDOjCCAiKgAwIBAgIUXx5dzBHJ7B9QmMEm5uyEiP6Fvl4wDQYJKoZIhvcNAQEL
BQAwHTEbMBkGA1UEAwwSbWVkaWEuZXhhbXBsZS50ZXN0MB4XDTI2MTAyODIxNTk0
MloXDTM2MTAyNTIxNTk0MlowHTEbMBkGA1UEAwwSbWVkaWEuZXhhbXBsZS50ZXN0
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApHHOTY/TQzM4rAvPNZPr
sADXzc5FHhyhcQ1NgBBTGqBr4uQYRx4njYj5TAY/CdzSSYENJ06RydcDRp7uhFat
j7RjdILiScX74O3cUOCkfPkIWKED5x9AJzdE1hDBqFLSOVJimjSvhT/oy1NSN5XR
Tcput8GK65yzMDHB96sDclqe85FVCFOGU8n/Vw7BGcFyr0cx2WMBL3eeJunS4X4D
OIpRicpBpXN/lH0MjMaM9cpStmeKx4goBEiejlaePDjN1TdPII06WiFEV4/VFGXe
U/M6r8iZKIxSln8BYttX1nPojsFd4tNMMf8Wl/EezkDDdvdYWbGr47RLGVA9UZ/G
IwIDAQABo3IwcDAdBgNVHQ4EFgQUiKq2x0u+Xra6TnwP16uCA108giQwHwYDVR0j
BBgwFoAUiKq2x0u+Xra6TnwP16uCA108giQwDwYDVR0TAQH/BAUwAwEB/zAdBgNV
HREEFjAUghJtZWRpYS5leGFtcGxlLnRlc3QwDQYJKoZIhvcNAQELBQADggEBAIkw
ANmcE4oA3U8zZ/njoHolx5hVTLUT37JV7DkDLifjpBPjY1GlyZGsMovA9hMkogzy
BMpvCqzGjhXX/Ib1+Ef6iC7rSzwJH/w9OyaZNEACZ58Ayf/8spR3xkuka7CiXREY
TjmLLBHNO79U8NqKFOCjwOrVdihwwQxmdYqTbUWw1q5iDHPs5KMFLOhDC7RYT+Gz
CTRG7ioWCzqnTO0rwIg4qTSKd15omD724FRu9l+HJa20QwlMIO2pYsqWaoT5rhT0
Ccos6H3OfTe7kwWdvEcOVHXsR+IDHM62k38FzMSpMVgOC3feNrrK9ldlN1qTU/GX
7o7WMKY8RcT5iTFsQ9w=
-----END CERTIFICATE-----
"""

_TEST_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCkcc5Nj9NDMzis
C881k+uwANfNzkUeHKFxDU2AEFMaoGvi5BhHHieNiPlMBj8J3NJJgQ0nTpHJ1wNG
nu6EVq2PtGN0guJJxfvg7dxQ4KR8+QhYoQPnH0AnN0TWEMGoUtI5UmKaNK+FP+jL
U1I3ldFNym63wYrrnLMwMcH3qwNyWp7zkVUIU4ZTyf9XDsEZwXKvRzHZYwEvd54m
6dLhfgM4ilGJykGlc3+UfQyMxoz1ylK2Z4rHiCgESJ6OVp48OM3VN08gjTpaIURX
j9UUZd5T8zqvyJkojFKWfwFi21fWc+iOwV3i00wx/xaX8R7OQMN291hZsavjtEsZ
UD1Rn8YjAgMBAAECggEACEYnpLRrY1K088cmVmJKdhEcyqCpzqGPJ/KM5zgrDkhr
FT/3zsBAKjltnbskudjQBd3F7SfhYv/8DLYbdzIrSnM/m9GoFCEvkro1HyamcBlR
p6/2U885OZ/oyUKPk+oS5Mp15p9nA3PbQTvAg8Z4gQsB9iH5XPCF/ZNLWKxUoJDE
6i0o//8E8vU0uIRjoR8+BbWp8R8+nNFbgoiY/xBmLfhFZRyZFyn8nI5gUqJM9FFG
NLqMjCHwfTynTIAnBcOIxe13OvVR8k4v+lBgcHnGPhEuRvVrs/r5cNwywCFwoeJM
RZoiSkyHP/KUzFOjZUQnQ3XG2VejxWkhBeSKi9DYZQKBgQDnNk8fwYV0O8n0Qe+i
PLNep53beATiLVRZ0G9F2+jbj1YyKRQqt2xTxcR08wcqDRXgBcSG0IVb878GWkLw
CHpHyUc6ggYNLmqmOugwI9CMQ2h94DmIjDp+ACcNJY3PaJkOs5qRd8qPWjhMVdvv
YS6k8VSJLq5Q0jmHDuEnIFn3xwKBgQC2Ewn8qzlJzF8/i9D4USrelQ7zBlOfS21X
/OFMQxPL/BUUSP6VpdxVklKDGrBlYlcrEmXRaSKAW3GaSZ/PIC0Il5V3/1WdOfza
3GJ9tPtukZ2JhbaHjfGH5azgsFeuwWUlF3seNhn4AJYr9+hpYz8HTOu5El1xHNJr
nQDF0B8WxQKBgFZh1vvxTE/ZyqEERWhir00M6JBHbeOmtd38XqZF++CT08tXx54R
+1ntCHH9Hk8apnKVFRCdNUXech8Id/u/3xeU3pTAYUxpbBUA6yCZMOBGivKEZFpc
sbnDitOBlVLJLG3usi/pBne6aFA0v7/Wmhurwj6ifyPY9v87kukQUCyfAoGBAIPc
gAQcYS4ANA8KjDn6VqjCnQQGVfNspfDuNKCNh73Yf8+kGbR5XXwfr2BBqPST5vtL
pK+ni5QYJcsOfQ19iP/LADXalr96apR6vV26/8kQ7l3Vl97OPtkjzjMfozDt4Pme
6sxzdBU3H4sSTKBefxwjaigrk77Cl+XV2+RnGMzdAoGASyGwTptlgXjsYovStxL6
IWH2AgnnXqgKkEMbOvvsM+Poo2XFbQ+L7FLc3sEbbUcYnsHn6TaJUvyrHWzvsZDS
QnJPFc7EivBVTUicpbOyiAQeVkfGihRIP0JE2hvvWvrw//zLQHWAE9HCpyo7ekUS
xzJiws5EnW7yAp+jjk0Enas=
-----END PRIVATE KEY-----
"""


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return

    def iter_bytes(self, chunk_size: int | None = None):
        yield json.dumps(self.payload).encode()


class _DnsClient:
    def __init__(self, payloads: dict[int, dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    @contextmanager
    def stream(self, method: str, url: str, *, params: dict[str, object]):
        self.calls.append({"url": url, **params})
        yield _Response(self.payloads[int(params["type"])])

    def close(self) -> None:
        return


def _record(kind: int, address: str, ttl: int = 120) -> dict[str, object]:
    return {"type": kind, "data": address, "TTL": ttl}


def _dns(
    answers: list[dict[str, object]], *, status: int = 0, **extra: object
) -> dict[str, object]:
    return {"Status": status, "Answer": answers, **extra}


def _make_resolver(
    payloads: dict[int, dict[str, object]],
) -> tuple[ProtectedResolver, _DnsClient]:
    resolver = ProtectedResolver("http://user:pass@127.0.0.1:3128")
    fake = _DnsClient(payloads)
    resolver._client.close()
    resolver._client = fake  # type: ignore[assignment]
    return resolver, fake


def _test_runtime() -> dict[int, dict[str, object]]:
    return {1: _dns([_record(1, "93.184.216.34")]), 28: _dns([])}


class ProtectedResolverTests(unittest.TestCase):
    def test_parses_both_families_and_caches_bounded_ttl(self) -> None:
        resolver, client = _make_resolver(
            {
                1: _dns([_record(1, "93.184.216.34", 300)]),
                28: _dns([_record(28, "2606:2800:220:1:248:1893:25c8:1946", 10)]),
            }
        )
        try:
            expected = (
                ipaddress.ip_address("93.184.216.34"),
                ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"),
            )
            self.assertEqual(resolver.resolve("Example.com."), expected)
            self.assertEqual(resolver.resolve("example.com"), expected)
            self.assertEqual(len(client.calls), 2)
        finally:
            resolver.close()

    def test_literal_addresses_do_not_use_doh(self) -> None:
        resolver, client = _make_resolver(_test_runtime())
        try:
            self.assertEqual(resolver.resolve("1.1.1.1"), (ipaddress.ip_address("1.1.1.1"),))
            with self.assertRaises(ValueError):
                resolver.resolve("127.0.0.1")
            with self.assertRaises(ValueError):
                resolver.resolve("service.internal")
            self.assertEqual(client.calls, [])
        finally:
            resolver.close()

    def test_fails_closed_for_unsafe_or_invalid_answers(self) -> None:
        invalid = (
            {1: _dns([_record(1, "93.184.216.34"), _record(1, "127.0.0.1")]), 28: _dns([])},
            {1: _dns([], status=2), 28: _dns([])},
            {1: _dns([], TC=True), 28: _dns([])},
            {1: {"Status": 0, "Answer": "bad"}, 28: _dns([])},
            {1: {"Status": 0, "Answer": None, "padding": "x" * 70_000}, 28: _dns([])},
            {1: _dns([]), 28: _dns([])},
        )
        for payloads in invalid:
            with self.subTest(payloads=payloads):
                resolver, _ = _make_resolver(payloads)
                try:
                    with self.assertRaises(ValueError):
                        resolver.resolve("example.com")
                finally:
                    resolver.close()

    def test_zero_ttl_answers_are_not_cached(self) -> None:
        resolver, client = _make_resolver({1: _dns([_record(1, "93.184.216.34", 0)]), 28: _dns([])})
        try:
            resolver.resolve("example.com")
            resolver.resolve("example.com")
            self.assertEqual(len(client.calls), 4)
        finally:
            resolver.close()

    def test_local_and_internal_names_are_rejected(self) -> None:
        for host in ("localhost", "x.local", "x.internal", "x.lan", "127.0.0.1", "bad..host"):
            with self.subTest(host=host):
                self.assertTrue(_host_is_forbidden(host))
        self.assertFalse(_host_is_forbidden("example.com"))


class _UpstreamSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.response = bytearray(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        result = self.response[:size]
        del self.response[:size]
        return bytes(result)

    def settimeout(self, timeout: float | None) -> None:
        return

    def shutdown(self, how: int) -> None:
        return

    def close(self) -> None:
        return


class _RelaySocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def recv(self, size: int) -> bytes:
        data = self.incoming[:size]
        del self.incoming[:size]
        return bytes(data)

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def shutdown(self, how: int) -> None:
        return


class _PendingTlsSocket(_RelaySocket):
    def pending(self) -> int:
        return len(self.incoming)


class GatewayUnitTests(unittest.TestCase):
    def test_relay_reads_decrypted_tls_bytes_pending_without_fd_readiness(self) -> None:
        client = _RelaySocket()
        upstream = _PendingTlsSocket(b"already-decrypted")
        with (
            patch("telegram_share_bot.proxy.gateway.ssl.SSLSocket", _PendingTlsSocket),
            patch("telegram_share_bot.proxy.gateway.select.select") as select_ready,
        ):
            select_ready.side_effect = [([], [], []), ([upstream], [], []), ([client], [], [])]
            ProxyGateway._relay(client, upstream)  # type: ignore[arg-type]
        self.assertEqual(client.sent, b"already-decrypted")
        self.assertEqual(select_ready.call_args_list[0].args[3], 0)
        self.assertEqual(select_ready.call_args_list[1].args[3], 15)

    def test_unauthenticated_client_receives_basic_proxy_challenge(self) -> None:
        gateway = ProxyGateway("http://127.0.0.1:3128", None)
        try:
            response = httpx.get(
                "http://example.com/resource",
                proxy=f"http://{gateway.client_url.rsplit('@', 1)[1]}",
                trust_env=False,
                timeout=3,
            )
            self.assertEqual(response.status_code, 407)
            self.assertEqual(
                response.headers["proxy-authenticate"],
                'Basic realm="telegram-share-bot"',
            )
        finally:
            gateway.close()

    def test_authenticates_and_connects_to_checked_ip_only(self) -> None:
        resolver, _ = _make_resolver(_test_runtime())
        gateway = ProxyGateway("http://proxy:password@127.0.0.1:3128", resolver)
        upstream = _UpstreamSocket()
        connect = Mock(return_value=upstream)
        try:
            with patch("telegram_share_bot.proxy.gateway.socket.create_connection", connect):
                address = ipaddress.ip_address("93.184.216.34")
                self.assertIs(gateway._connect_target(80, (address,)), upstream)
            self.assertEqual(connect.call_args.args[0], ("127.0.0.1", 3128))
            self.assertIn(b"CONNECT 93.184.216.34:80 HTTP/1.1\r\n", upstream.sent)
            self.assertIn(b"Proxy-Authorization: Basic ", upstream.sent)
            self.assertIn("@127.0.0.1:", gateway.client_url)

            handler_type = gateway._handler_type()
            handler = handler_type.__new__(handler_type)
            handler.headers = Message()
            handler.headers["Proxy-Authorization"] = ""
            errors: list[int] = []
            handler._error = errors.append
            handler._handle()
            self.assertEqual(errors, [407])
        finally:
            gateway.close()
            resolver.close()

    def test_does_not_fall_back_when_upstream_fails(self) -> None:
        resolver, fake_dns = _make_resolver(_test_runtime())
        gateway = ProxyGateway("http://127.0.0.1:3128", resolver)
        attempted: list[object] = []

        def refused(address, **kwargs):
            attempted.append(address)
            raise OSError("proxy offline")

        try:
            with patch("telegram_share_bot.proxy.gateway.socket.create_connection", refused):
                with self.assertRaises(OSError):
                    gateway._connect_target(80, (ipaddress.ip_address("93.184.216.34"),))
            self.assertEqual(attempted, [("127.0.0.1", 3128)])
            self.assertEqual(fake_dns.calls, [])
        finally:
            gateway.close()
            resolver.close()

    def test_rejects_private_upstream_destination_ip(self) -> None:
        resolver, _ = _make_resolver(_test_runtime())
        gateway = ProxyGateway("http://127.0.0.1:3128", resolver)
        try:
            with self.assertRaises(ValueError):
                gateway._connect_target(80, (ipaddress.ip_address("10.0.0.1"),))
        finally:
            gateway.close()
            resolver.close()

    def test_dns_opt_out_forwards_hostname_without_doh(self) -> None:
        gateway = ProxyGateway("http://proxy:password@127.0.0.1:3128", None)
        upstream = _UpstreamSocket()
        connect = Mock(return_value=upstream)
        try:
            with patch("telegram_share_bot.proxy.gateway.socket.create_connection", connect):
                gateway._connect_target(443, ("media.example.test",))
            self.assertIn(b"CONNECT media.example.test:443 HTTP/1.1\r\n", upstream.sent)
            with self.assertRaises(ValueError):
                gateway._connect_target(443, (ipaddress.ip_address("10.0.0.1"),))
        finally:
            gateway.close()


def _read_headers(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(1)
        if not part:
            break
        data.extend(part)
    return bytes(data)


class _TlsOrigin(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        tls_socket = server.ssl_context.wrap_socket(self.request, server_side=True)
        request = _read_headers(tls_socket)
        server.requests.append(request)
        if request.startswith(b"GET /redirect "):
            location = server.private_redirect_url.encode("ascii")
            tls_socket.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: "
                + location
                + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        else:
            tls_socket.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
        tls_socket.close()


class _RecordingProxy(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        request = _read_headers(self.request)
        server = self.server
        server.connect_targets.append(request.split(b"\r\n", 1)[0].decode("ascii"))
        if server.refuse:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        origin = socket.create_connection(server.origin_address, timeout=2)
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        peers = {self.request: origin, origin: self.request}
        alive = set(peers)
        while alive:
            readable, _, _ = select.select(tuple(alive), (), [], 2)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    alive.discard(source)
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    peers[source].sendall(data)
        origin.close()


def _start_server(
    handler: type[socketserver.BaseRequestHandler],
) -> tuple[socketserver.ThreadingTCPServer, threading.Thread]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server: socketserver.ThreadingTCPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class GatewayTlsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        cert_path = Path(self.temp_dir.name) / "proxy-test-cert.pem"
        key_path = Path(self.temp_dir.name) / "proxy-test-key.pem"
        cert_path.write_text(_TEST_CERTIFICATE, encoding="ascii")
        key_path.write_text(_TEST_PRIVATE_KEY, encoding="ascii")

        self.origin, self.origin_thread = _start_server(_TlsOrigin)
        self.origin.requests = []
        self.origin.sni_names = []
        self.origin.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.origin.ssl_context.load_cert_chain(cert_path, key_path)
        self.origin.ssl_context.set_servername_callback(
            lambda sock, name, context: self.origin.sni_names.append(name)
        )
        self.proxy, self.proxy_thread = _start_server(_RecordingProxy)
        self.proxy.origin_address = self.origin.server_address
        self.proxy.connect_targets = []
        self.proxy.refuse = False
        self.origin.private_redirect_url = (
            f"http://127.0.0.1:{self.proxy.server_address[1]}/private"
        )

        self.resolver, _ = _make_resolver(_test_runtime())
        self.gateway = ProxyGateway(
            f"http://127.0.0.1:{self.proxy.server_address[1]}", self.resolver
        )
        self.client_context = ssl.create_default_context(cadata=_TEST_CERTIFICATE)

    def tearDown(self) -> None:
        self.gateway.close()
        self.resolver.close()
        _stop_server(self.proxy, self.proxy_thread)
        _stop_server(self.origin, self.origin_thread)
        self.temp_dir.cleanup()

    def test_connect_keeps_tls_hostname_and_sni_while_proxy_gets_literal_ip(self) -> None:
        response = httpx.get(
            "https://media.example.test/asset?signature=secret",
            proxy=self.gateway.client_url,
            verify=self.client_context,
            trust_env=False,
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(self.origin.sni_names, ["media.example.test"])
        self.assertTrue(
            self.proxy.connect_targets[0].startswith("CONNECT 93.184.216.34:443 HTTP/1.1")
        )
        self.assertTrue(self.origin.requests[0].startswith(b"GET /asset?signature=secret HTTP/1.1"))

    def test_upstream_failure_returns_502_without_origin_fallback(self) -> None:
        self.proxy.refuse = True
        with self.assertRaises(httpx.ProxyError):
            httpx.get(
                "https://media.example.test/asset",
                proxy=self.gateway.client_url,
                verify=self.client_context,
                trust_env=False,
                timeout=5,
            )
        self.assertEqual(len(self.origin.requests), 0)
        self.assertEqual(len(self.proxy.connect_targets), 1)

    def test_dns_opt_out_blocks_private_ip_redirect(self) -> None:
        runtime = ProxyRuntime(
            f"http://127.0.0.1:{self.proxy.server_address[1]}", dns_protection=False
        )
        try:
            response = httpx.get(
                "https://media.example.test/redirect",
                proxy=runtime.client_proxy_url,
                verify=self.client_context,
                trust_env=False,
                follow_redirects=True,
                timeout=5,
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(len(self.origin.requests), 1)
            self.assertEqual(
                self.proxy.connect_targets,
                ["CONNECT media.example.test:443 HTTP/1.1"],
            )
        finally:
            runtime.close()


class ProxyRuntimeTests(unittest.TestCase):
    def test_configuration_and_close_are_idempotent(self) -> None:
        first = configure_proxy("http://127.0.0.1:3128", dns_protection=False)
        self.assertIsNotNone(first)
        self.assertIs(configure_proxy("http://127.0.0.1:3128", dns_protection=False), first)
        self.assertIs(get_proxy_runtime(), first)
        self.assertIn("@127.0.0.1:", first.client_proxy_url)
        self.assertNotIn(":3128", first.client_proxy_url)
        self.assertEqual(first.dns_url, DEFAULT_DNS_URL)
        close_proxy(first)
        close_proxy(first)
        self.assertIsNone(get_proxy_runtime())

    def test_disabled_runtime_does_not_resolve_addresses(self) -> None:
        runtime = ProxyRuntime("http://127.0.0.1:3128", dns_protection=False)
        with self.assertRaises(RuntimeError):
            runtime.resolve_addresses("example.com")
        runtime.close()

    def test_no_proxy_is_cleared_while_runtime_is_active_and_restored(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "localhost,.internal"}):
            http_proxy = os.environ.get("HTTP_PROXY")
            runtime = ProxyRuntime("http://127.0.0.1:3128", dns_protection=False)
            try:
                self.assertFalse(any(key.lower() == "no_proxy" for key in os.environ))
                self.assertEqual(os.environ.get("HTTP_PROXY"), http_proxy)
            finally:
                runtime.close()
            no_proxy = [
                (key, value) for key, value in os.environ.items() if key.lower() == "no_proxy"
            ]
            self.assertEqual(len(no_proxy), 1)
            self.assertEqual(no_proxy[0][1], "localhost,.internal")

    def test_no_proxy_restore_does_not_overwrite_concurrent_change(self) -> None:
        with patch.dict(os.environ, {"no_proxy": "original"}):
            runtime = ProxyRuntime("http://127.0.0.1:3128", dns_protection=False)
            os.environ["NO_PROXY"] = "operator-change"
            runtime.close()
            current = next(value for key, value in os.environ.items() if key.lower() == "no_proxy")
            self.assertEqual(current, "operator-change")

    def test_no_proxy_stays_cleared_until_last_runtime_closes(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "localhost"}):
            first = ProxyRuntime("http://127.0.0.1:3128", dns_protection=False)
            second = ProxyRuntime("http://127.0.0.1:3129", dns_protection=False)
            try:
                first.close()
                self.assertFalse(any(key.lower() == "no_proxy" for key in os.environ))
            finally:
                second.close()
            self.assertTrue(any(key.lower() == "no_proxy" for key in os.environ))

    def test_construction_failure_restores_no_proxy(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "localhost"}):
            with patch(
                "telegram_share_bot.proxy.runtime.ProxyGateway", side_effect=OSError("bind failed")
            ):
                with self.assertRaisesRegex(OSError, "bind failed"):
                    ProxyRuntime("http://127.0.0.1:3128")
            no_proxy = next(value for key, value in os.environ.items() if key.lower() == "no_proxy")
            self.assertEqual(no_proxy, "localhost")

    def test_no_configured_proxy_leaves_no_proxy_unchanged(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "keep-me", "HTTP_PROXY": ""}):
            self.assertIsNone(configure_proxy(None))
            self.assertEqual(
                next(value for key, value in os.environ.items() if key.lower() == "no_proxy"),
                "keep-me",
            )
