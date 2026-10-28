"""Authenticated loopback HTTP gateway that pins destinations through an upstream proxy."""

from __future__ import annotations

import base64
import hmac
import http.client
import ipaddress
import logging
import re
import secrets
import select
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from telegram_share_bot.proxy.resolver import Address, ProtectedResolver

logger = logging.getLogger(__name__)
_MAX_HEADER = 32 * 1024
_MAX_CONNECTIONS = 64
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 15
_TUNNEL_IDLE_TIMEOUT = 180
_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_HEADERS = {
    b"connection",
    b"proxy-connection",
    b"proxy-authorization",
    b"keep-alive",
    b"te",
    b"trailer",
    b"upgrade",
}
_ServerRequest = socket.socket | tuple[bytes, socket.socket]


class _BoundedHeaderReader:
    def __init__(self, reader: Any, limit: int) -> None:
        self._reader = reader
        self._remaining = limit

    def readline(self, size: int = -1) -> bytes:
        bound = self._remaining + 1
        line: bytes = self._reader.readline(bound if size < 0 else min(size, bound))
        self._remaining -= len(line)
        if self._remaining < 0:
            raise http.client.HTTPException("Request headers are too large")
        return line


def _host_is_forbidden(host: str) -> bool:
    from telegram_share_bot.media.security import _is_public_hostname_syntax

    return not _is_public_hostname_syntax(host)


def _is_safe_ip(ip: Address) -> bool:
    from telegram_share_bot.media.security import _is_safe_ip as check

    return check(ip)


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        runtime: ProxyGateway,
    ) -> None:
        self.runtime = runtime
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._sockets_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        super().__init__(server_address, handler)

    def process_request(self, request: _ServerRequest, client_address: Any) -> None:
        request_socket = request[1] if isinstance(request, tuple) else request
        if not self._slots.acquire(blocking=False):
            try:
                request_socket.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._sockets_lock:
            self._active_sockets.add(request_socket)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._sockets_lock:
                self._active_sockets.discard(request_socket)
            self._slots.release()
            raise

    def process_request_thread(self, request: _ServerRequest, client_address: Any) -> None:
        request_socket = request[1] if isinstance(request, tuple) else request
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._sockets_lock:
                self._active_sockets.discard(request_socket)
            self._slots.release()

    def close_active(self) -> None:
        with self._sockets_lock:
            sockets = tuple(self._active_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(_READ_TIMEOUT)
        return request, client_address

    def track_socket(self, sock: socket.socket) -> None:
        with self._sockets_lock:
            self._active_sockets.add(sock)

    def forget_socket(self, sock: socket.socket) -> None:
        with self._sockets_lock:
            self._active_sockets.discard(sock)

    def handle_error(self, request: _ServerRequest, client_address: Any) -> None:
        # Avoid socketserver's traceback, whose attached exception data can contain URLs.
        logger.warning("Local proxy request failed")


class ProxyGateway:
    """Owns the local authenticated proxy endpoint and its upstream resolver."""

    def __init__(self, proxy_url: str, resolver: ProtectedResolver | None) -> None:
        self._resolver = resolver
        self._upstream = urlsplit(proxy_url)
        if self._upstream.scheme not in {"http", "https"} or not self._upstream.hostname:
            raise ValueError("HTTP proxy URL must use http or https and include a host")
        self._upstream_host = self._upstream.hostname
        self._upstream_port = self._upstream.port or (
            443 if self._upstream.scheme == "https" else 80
        )
        self._auth_token = (
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        )
        self._expected_auth = (
            "Basic " + base64.b64encode(f"proxy:{self._auth_token}".encode()).decode()
        )
        self._server = _GatewayServer(("127.0.0.1", 0), self._handler_type(), self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="proxy-gateway", daemon=True
        )
        self._closed = False
        self._lock = threading.Lock()
        self._thread.start()

    @property
    def client_url(self) -> str:
        return f"http://proxy:{self._auth_token}@127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._server.shutdown()
        self._server.close_active()
        self._server.server_close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            rbufsize = 0
            raw_requestline: bytes

            def parse_request(self) -> bool:
                reader = self.rfile
                self.rfile = _BoundedHeaderReader(reader, _MAX_HEADER)  # type: ignore[assignment]
                try:
                    return super().parse_request()
                finally:
                    self.rfile = reader

            def log_message(self, fmt: str, *args: object) -> None:
                # Request lines can contain signed/tokenized URLs.
                logger.debug("Local proxy request completed")

            def do_CONNECT(self) -> None:
                self._handle(connect=True)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

            def do_HEAD(self) -> None:
                self._handle()

            def do_OPTIONS(self) -> None:
                self._handle()

            def _error(self, code: int) -> None:
                try:
                    self.send_response(code)
                    if code == 407:
                        self.send_header("Proxy-Authenticate", 'Basic realm="telegram-share-bot"')
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass
                self.close_connection = True

            def _handle(self, connect: bool = False) -> None:
                auth_values = self.headers.get_all("Proxy-Authorization", []) or []
                if len(auth_values) != 1 or not hmac.compare_digest(
                    auth_values[0], runtime._expected_auth
                ):
                    self._error(407)
                    return
                try:
                    host, port, request_line, headers = self._parse_target(connect)
                    if _host_is_forbidden(host):
                        self._error(403)
                        return
                    addresses: tuple[Address | str, ...]
                    try:
                        literal = ipaddress.ip_address(host.strip("[]"))
                    except ValueError:
                        literal = None
                    if literal is not None:
                        addresses = (literal,)
                    else:
                        if runtime._resolver is not None:
                            addresses = runtime._resolver.resolve(host)
                        else:
                            addresses = (host,)
                    upstream = runtime._connect_target(port, addresses)
                except ValueError:
                    self._error(400)
                    return
                except Exception:
                    # Do not log exceptions: httpx/socket errors can contain credentialed URLs.
                    self._error(502)
                    return

                try:
                    if connect:
                        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        self.wfile.flush()
                    else:
                        upstream.sendall(request_line + headers)
                    self.connection.settimeout(_READ_TIMEOUT)
                    upstream.settimeout(_READ_TIMEOUT)
                    runtime._relay(self.connection, upstream)
                except (OSError, TimeoutError):
                    pass
                finally:
                    try:
                        upstream.close()
                    except OSError:
                        pass
                    runtime._server.forget_socket(upstream)
                    self.close_connection = True

            def _parse_target(self, connect: bool) -> tuple[str, int, bytes, bytes]:
                if connect:
                    raw_headers = self._read_headers()
                    self._validate_body_headers(raw_headers)
                    if any(
                        name.lower() == b"transfer-encoding"
                        or (
                            name.lower() == b"content-length" and value.strip() not in {b"0", b"+0"}
                        )
                        for name, value in (
                            line.split(b":", 1) for line in raw_headers.splitlines()
                        )
                    ):
                        raise ValueError("CONNECT requests cannot carry a body")
                    authority = self.path
                    if authority.startswith("["):
                        close = authority.find("]")
                        if close < 0 or authority[close + 1 : close + 2] != ":":
                            raise ValueError("Malformed CONNECT target")
                        host, port_text = authority[1:close], authority[close + 2 :]
                    else:
                        if authority.count(":") != 1:
                            raise ValueError("Malformed CONNECT target")
                        host, port_text = authority.rsplit(":", 1)
                    port = int(port_text)
                    if not 1 <= port <= 65535:
                        raise ValueError("Invalid target port")
                    return host, port, b"", b""

                parsed = urlsplit(self.path)
                if parsed.scheme.lower() != "http" or not parsed.hostname:
                    raise ValueError("Expected an absolute HTTP request target")
                if parsed.username or parsed.password:
                    raise ValueError("Credentials in destination URL are not accepted")
                host = parsed.hostname
                port = parsed.port if parsed.port is not None else 80
                if not 1 <= port <= 65535:
                    raise ValueError("Invalid target port")
                raw_headers = self._read_headers()
                self._validate_body_headers(raw_headers)
                self._validate_host_header(raw_headers, host, port)
                first_line = self.raw_requestline
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                request_line = (
                    first_line.split(b" ", 2)[0] + b" " + path.encode("ascii") + b" HTTP/1.1\r\n"
                )
                return host, port, request_line, self._sanitize_headers(raw_headers, host, port)

            def _read_headers(self) -> bytes:
                lines: list[bytes] = []
                total = 0
                for count, (name, value) in enumerate(self.headers.raw_items(), start=1):
                    if count > 100:
                        raise ValueError("Too many headers")
                    try:
                        line = name.encode("ascii") + b": " + value.encode("latin-1") + b"\r\n"
                    except UnicodeError as exc:
                        raise ValueError("Malformed header") from exc
                    total += len(line)
                    if total > _MAX_HEADER:
                        raise ValueError("Malformed or oversized headers")
                    lines.append(line)
                return b"".join(lines)

            def _validate_body_headers(self, raw: bytes) -> None:
                content_lengths: list[bytes] = []
                transfer_encodings: list[bytes] = []
                for line in raw.splitlines():
                    name, value = line.split(b":", 1)
                    if name.lower() == b"content-length":
                        content_lengths.append(value.strip())
                    elif name.lower() == b"transfer-encoding":
                        transfer_encodings.append(value.strip().lower())
                if len(content_lengths) > 1 or len(transfer_encodings) > 1:
                    raise ValueError("Ambiguous body framing")
                if content_lengths and transfer_encodings:
                    raise ValueError("Conflicting body framing")
                if content_lengths:
                    try:
                        if int(content_lengths[0]) < 0:
                            raise ValueError("Invalid content length")
                    except ValueError as exc:
                        raise ValueError("Invalid content length") from exc
                if transfer_encodings and transfer_encodings[0] != b"chunked":
                    raise ValueError("Unsupported transfer encoding")

            def _validate_host_header(self, raw: bytes, target_host: str, target_port: int) -> None:
                values = [
                    value.strip()
                    for line in raw.splitlines()
                    if line.split(b":", 1)[0].lower() == b"host"
                    for value in [line.split(b":", 1)[1].decode("latin-1")]
                ]
                if len(values) != 1:
                    raise ValueError("Exactly one Host header is required")
                parsed = urlsplit("//" + values[0])
                if (
                    not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("Invalid Host header")
                host_port = parsed.port if parsed.port is not None else 80
                if host_port != target_port:
                    raise ValueError("Host header port does not match request target")
                try:
                    header_host = ipaddress.ip_address(parsed.hostname).compressed
                    actual_host = ipaddress.ip_address(target_host).compressed
                except ValueError:
                    try:
                        header_host = (
                            parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
                        )
                        actual_host = target_host.encode("idna").decode("ascii").lower().rstrip(".")
                    except UnicodeError as exc:
                        raise ValueError("Invalid Host header") from exc
                if header_host != actual_host:
                    raise ValueError("Host header does not match request target")

            def _sanitize_headers(self, raw: bytes, host: str, port: int) -> bytes:
                parsed: list[tuple[bytes, bytes]] = []
                connection_tokens: set[bytes] = set()
                for line in raw.splitlines():
                    if b":" not in line:
                        raise ValueError("Malformed header")
                    name, value = line.split(b":", 1)
                    value = value.strip(b" \t")
                    if not _TOKEN_RE.fullmatch(name) or any(
                        (ch < 32 and ch != 9) or ch == 127 for ch in value
                    ):
                        raise ValueError("Invalid header")
                    if name.lower() == b"connection":
                        connection_tokens.update(
                            token.strip().lower() for token in value.split(b",")
                        )
                    parsed.append((name, value))
                kept = [
                    name + b": " + value + b"\r\n"
                    for name, value in parsed
                    if name.lower() not in _HOP_HEADERS
                    and name.lower() not in connection_tokens
                    and name.lower() != b"host"
                ]
                host_name = f"[{host}]" if ":" in host else host
                host_suffix = f":{port}" if port != 80 else ""
                normalized_host = f"Host: {host_name}{host_suffix}\r\n".encode("ascii")
                return normalized_host + b"".join(kept) + b"Connection: close\r\n\r\n"

        return Handler

    def _connect_target(self, port: int, addresses: tuple[Address | str, ...]) -> socket.socket:
        errors: list[Exception] = []
        for address in addresses:
            if isinstance(address, str):
                if _host_is_forbidden(address):
                    raise ValueError("Unsafe destination host")
                destination = address.encode("idna").decode("ascii")
            else:
                if not _is_safe_ip(address):
                    raise ValueError("Unsafe destination address")
                destination = address.compressed
            upstream: socket.socket | None = None
            try:
                upstream = socket.create_connection(
                    (self._upstream_host, self._upstream_port), timeout=_CONNECT_TIMEOUT
                )
                if self._upstream.scheme == "https":
                    context = ssl.create_default_context()
                    upstream = context.wrap_socket(upstream, server_hostname=self._upstream_host)
                if not isinstance(address, str) and address.version == 6:
                    destination = f"[{destination}]"
                authority = f"{destination}:{port}"
                username = unquote(self._upstream.username or "")
                password = unquote(self._upstream.password or "")
                proxy_auth = ""
                if self._upstream.username is not None:
                    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
                    proxy_auth = f"Proxy-Authorization: Basic {credentials}\r\n"
                request = (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n{proxy_auth}Connection: keep-alive\r\n\r\n"
                ).encode("ascii")
                upstream.sendall(request)
                response = _read_response_headers(upstream)
                status = response.split(b"\r\n", 1)[0].split()
                if len(status) < 2 or status[1] != b"200":
                    raise OSError("Upstream proxy refused destination")
                self._server.track_socket(upstream)
                return upstream
            except Exception as exc:
                if upstream is not None:
                    try:
                        upstream.close()
                    except OSError:
                        pass
                    self._server.forget_socket(upstream)
                errors.append(exc)
        raise OSError("No protected destination connection succeeded") from (
            errors[-1] if errors else None
        )

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        # Handler.rbufsize=0 ensures no client body or TLS bytes were prefetched.
        peers = {left: right, right: left}
        alive = set(peers)
        last_activity = time.monotonic()
        while alive and time.monotonic() - last_activity <= _TUNNEL_IDLE_TIMEOUT:
            readable, _, _ = select.select(tuple(alive), (), (), _READ_TIMEOUT)
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(65536)
                except (OSError, TimeoutError):
                    return
                if not data:
                    alive.discard(source)
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                last_activity = time.monotonic()
                try:
                    peers[source].sendall(data)
                except OSError:
                    return


def _read_response_headers(sock: socket.socket) -> bytes:
    received = bytearray()
    while len(received) <= _MAX_HEADER:
        byte = sock.recv(1)
        if not byte:
            break
        received.extend(byte)
        if received.endswith(b"\r\n\r\n"):
            return bytes(received)
    raise OSError("Invalid upstream proxy response")
