"""DNS-pinned HTTP transports for source, extractor, and preview requests."""

from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
import yt_dlp
from curl_cffi.const import CurlOpt
from curl_cffi.requests import Session
from httpcore._backends.anyio import AnyIOBackend
from yt_dlp.networking._curlcffi import CurlCFFIRH  # type: ignore[import-not-found]
from yt_dlp.networking.exceptions import RequestError

from telegram_share_bot.media.security import resolve_safe_addresses

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "host", "proxy-authorization", "referer"}
)
_BODY_HEADERS = frozenset({"content-length", "content-type", "transfer-encoding"})


def _authority(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RequestError("Invalid media request URL") from exc
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise RequestError("Unsafe media request URL")
    if port == 0:
        raise RequestError("Media request URL has an invalid port")
    default_port = 443 if scheme == "https" else 80
    return hostname, default_port if port is None else port


def _checked_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        return resolve_safe_addresses(hostname)
    except (OSError, ValueError) as exc:
        raise RequestError("Media host resolved to an unsafe or unavailable address") from exc


def _curl_resolve_entry(
    hostname: str,
    port: int,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str:
    host = hostname.encode("idna").decode("ascii")
    ip = f"[{address}]" if address.version == 6 else str(address)
    return f"{host}:{port}:{ip}"


def _same_origin(left: str, right: str) -> bool:
    left_host, left_port = _authority(left)
    right_host, right_port = _authority(right)
    return (
        urlsplit(left).scheme.lower() == urlsplit(right).scheme.lower()
        and left_host.lower().rstrip(".") == right_host.lower().rstrip(".")
        and left_port == right_port
    )


def _strip_redirect_headers(headers: Any, *, preserve_body: bool) -> Any:
    if headers is None:
        return None
    blocked = _SENSITIVE_HEADERS if preserve_body else _SENSITIVE_HEADERS | _BODY_HEADERS
    if isinstance(headers, Mapping):
        return {key: value for key, value in headers.items() if str(key).lower() not in blocked}
    if isinstance(headers, list):
        return [
            (key, value)
            for key, value in headers
            if str(key).lower() not in blocked
        ]
    return headers


class _PinnedCurlSession(Session[Any]):
    """curl-cffi session that validates and pins every HTTP redirect hop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resolve_lock = threading.Lock()

    def request(self, method: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        current_method = method.upper()
        current_url = url
        current_kwargs = dict(kwargs)
        current_kwargs["allow_redirects"] = False
        current_kwargs["proxies"] = {}
        current_kwargs["proxy"] = None
        current_kwargs.pop("max_redirects", None)
        current_kwargs["stream"] = True

        for redirects_followed in range(_MAX_REDIRECTS + 1):
            hostname, port = _authority(current_url)
            addresses = _checked_addresses(hostname)
            with self._resolve_lock:
                try:
                    ipaddress.ip_address(hostname)
                except ValueError:
                    # CURLOPT_RESOLVE replaces DNS while retaining the original
                    # URL host, HTTP Host header, certificate validation, and
                    # TLS SNI. curl-cffi clones this handle for streaming; guard
                    # the setopt/clone/reset sequence against concurrent calls.
                    self.curl.setopt(
                        CurlOpt.RESOLVE,
                        [_curl_resolve_entry(hostname, port, addresses[0])],
                    )

                response = super().request(
                    current_method,
                    current_url,
                    *args,
                    **current_kwargs,
                )
            location = response.headers.get("location")
            if response.status_code not in _REDIRECT_STATUSES or not location:
                return response
            if redirects_followed >= _MAX_REDIRECTS:
                response.close()
                raise RequestError("Media request exceeded the redirect limit")

            next_url = urljoin(current_url, location)
            same_origin = _same_origin(current_url, next_url)
            if not same_origin:
                current_kwargs["headers"] = _strip_redirect_headers(
                    current_kwargs.get("headers"),
                    preserve_body=response.status_code in {307, 308},
                )
                current_kwargs.pop("auth", None)
                current_kwargs.pop("cookies", None)
                current_kwargs.pop("referer", None)

            if response.status_code == 303 or (
                response.status_code in {301, 302} and current_method not in {"GET", "HEAD"}
            ):
                current_method = "GET"
                for key in ("data", "content", "json", "files"):
                    current_kwargs.pop(key, None)
                current_kwargs["headers"] = _strip_redirect_headers(
                    current_kwargs.get("headers"), preserve_body=False
                )

            response.close()
            current_url = next_url

        raise RequestError("Media request exceeded the redirect limit")


class GuardedCurlCFFIRH(CurlCFFIRH):  # type: ignore[misc]
    """yt-dlp curl-cffi handler with direct-only, DNS-pinned connections."""

    RH_KEY = CurlCFFIRH.RH_KEY

    def _create_instance(self, cookiejar: Any = None) -> _PinnedCurlSession:
        return _PinnedCurlSession(cookies=cookiejar, trust_env=False, allow_redirects=False)

    def _send(self, request: Any) -> Any:
        proxies = self._get_proxies(request)
        if any(value for key, value in proxies.items() if key != "no"):
            raise RequestError("Outbound proxies are unsupported by the guarded transport")
        return super()._send(request)


class GuardedYoutubeDL(yt_dlp.YoutubeDL):
    """yt-dlp with guarded curl requests and no proxy-based bypass path."""

    def build_request_director(self, handlers: Any, preferences: Any = None) -> Any:
        guarded_handlers = tuple(
            GuardedCurlCFFIRH if handler is CurlCFFIRH else handler
            for handler in handlers
        )
        director = super().build_request_director(cast(Any, guarded_handlers), preferences)
        curl_handler = director.handlers.get(CurlCFFIRH.RH_KEY)
        if not isinstance(curl_handler, GuardedCurlCFFIRH):
            raise RuntimeError("The guarded media transport requires yt-dlp curl-cffi support")
        if any(
            value
            for handler in director.handlers.values()
            for key, value in getattr(handler, "proxies", {}).items()
            if key != "no"
        ):
            raise RuntimeError("Outbound proxy settings are unsupported by the guarded transport")
        return director


def create_youtube_dl(options: dict[str, Any]) -> yt_dlp.YoutubeDL:
    """Create yt-dlp with the guarded native transport installed."""
    return GuardedYoutubeDL(options)  # type: ignore[arg-type]


class _GuardedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Connect HTTPX to a validated address while keeping the URL hostname for TLS."""

    def __init__(self) -> None:
        self._delegate = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[Any, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        started = time.monotonic()
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(_checked_addresses, host), timeout=timeout
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Timed out resolving media host") from exc
        except RequestError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        last_error: httpcore.ConnectError | None = None
        for address in addresses:
            remaining = (
                None
                if timeout is None
                else max(0.001, timeout - (time.monotonic() - started))
            )
            try:
                return await self._delegate.connect_tcp(
                    str(address),
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Media host has no safe addresses")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[Any, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix socket destinations are unsupported")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class GuardedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that pins every new connection to a validated DNS result."""

    def __init__(self) -> None:
        super().__init__(trust_env=False, proxy=None, retries=0)
        # httpx 0.27-0.28 does not expose httpcore's network_backend parameter.
        # The project's bounded httpx dependency range and transport tests cover
        # this single internal seam; the connection pool itself remains httpcore's.
        self._pool._network_backend = _GuardedAsyncBackend()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            _authority(str(request.url))
        except RequestError as exc:
            raise httpx.ConnectError("Unsafe preview request URL", request=request) from exc
        return await super().handle_async_request(request)
