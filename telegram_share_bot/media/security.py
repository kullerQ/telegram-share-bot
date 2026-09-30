"""URL validation and DNS pinning for media requests."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
from collections.abc import Generator, Sequence
from typing import Any
from urllib.parse import urlsplit

from telegram_share_bot.platforms.urls import has_url_credentials, safe_url_for_log

logger = logging.getLogger(__name__)

# Shared address space (CGNAT / some VPN overlays) — not covered by is_private.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


def _is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_safe_ip(ip.ipv4_mapped)

    if str(ip) == "169.254.169.254":
        return False

    for network in _BLOCKED_NETWORKS:
        if ip in network:
            return False

    return True


def is_safe_media_url(url: str, *, https_only: bool = False) -> bool:
    """Validate an HTTP(S) URL is not internal/private/loopback/cloud-metadata."""
    try:
        if has_url_credentials(url):
            return False
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if https_only:
            if scheme != "https":
                return False
        elif scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        hostname_clean = hostname.strip().lower().rstrip(".")
        if not hostname_clean:
            return False

        if hostname_clean == "localhost" or hostname_clean.endswith(
            (".localhost", ".local", ".internal", ".lan")
        ):
            return False

        try:
            ip = ipaddress.ip_address(hostname_clean)
            return _is_safe_ip(ip)
        except ValueError:
            pass

        addr_info = socket.getaddrinfo(hostname_clean, None, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False

        for res in addr_info:
            sockaddr = res[4]
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if not _is_safe_ip(ip):
                return False

        return True
    except Exception as exc:
        logger.warning("URL security check rejected %s: %s", safe_url_for_log(url), exc)
        return False


def is_allowed_media_host(url: str, allowed_hosts: frozenset[str] | None) -> bool:
    """Check whether the URL hostname matches the configured host allowlist.

    ``allowed_hosts is None`` means any host is permitted. Matching is
    suffix-based (``video.tiktok.com`` matches ``tiktok.com``).
    """
    if allowed_hosts is None:
        return True
    try:
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        host = hostname.strip().lower().rstrip(".").removeprefix("www.")
        if not host:
            return False
        for allowed in allowed_hosts:
            if host == allowed or host.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False


def is_https_url(url: str) -> bool:
    """Return whether a URL uses HTTPS."""
    return urlsplit(url).scheme.lower() == "https"


# True libc/resolver getaddrinfo — captured once so concurrent download threads
# never nest or uninstall each other's wrappers.
_REAL_GETADDRINFO = socket.getaddrinfo
_dns_guard_tls = threading.local()
_dns_guard_install_lock = threading.Lock()
_dns_guard_installed = False


def _dns_host_key(host: str | bytes | None) -> str | None:
    if host is None:
        return None
    text: str
    if isinstance(host, bytes):
        try:
            text = host.decode("idna")
        except UnicodeError:
            text = host.decode("utf-8", errors="replace")
    else:
        text = host
    cleaned = text.strip().lower().rstrip(".")
    return cleaned or None


def _guarded_getaddrinfo(
    host: str | bytes | None,
    port: str | bytes | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[Any, ...]]:
    state: dict[str, Any] | None = getattr(_dns_guard_tls, "state", None)
    if state is None:
        return _REAL_GETADDRINFO(host, port, family, type, proto, flags)

    key = _dns_host_key(host)
    results = _REAL_GETADDRINFO(host, port, family, type, proto, flags)
    safe_results: list[tuple[Any, ...]] = []
    for res in results:
        sockaddr = res[4]
        if not isinstance(sockaddr, Sequence) or not sockaddr:
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if not _is_safe_ip(ip):
            raise OSError(f"Blocked unsafe address for host {host!r}: {ip}")
        safe_results.append(res)

    if not safe_results:
        raise OSError(f"No safe addresses for host {host!r}")

    if key is None:
        return safe_results

    pinned_ips: dict[str, str] = state["pinned_ips"]
    pinned = pinned_ips.get(key)
    if pinned is not None:
        pinned_results = [
            res for res in safe_results if str(ipaddress.ip_address(res[4][0])) == pinned
        ]
        if pinned_results:
            return pinned_results
        # CDN / anycast hosts rotate A/AAAA sets within a single download.
        # Re-pin to a newly observed safe address instead of failing the request.

    pinned_ips[key] = str(ipaddress.ip_address(safe_results[0][4][0]))
    return [safe_results[0]]


def _ensure_dns_guard_installed() -> None:
    global _dns_guard_installed
    if _dns_guard_installed:
        return
    with _dns_guard_install_lock:
        if _dns_guard_installed:
            return
        socket.getaddrinfo = _guarded_getaddrinfo
        _dns_guard_installed = True


@contextlib.contextmanager
def _safe_dns_resolution() -> Generator[None, None, None]:
    """Re-validate DNS lookups and prefer a stable safe IP per host.

    Blocks private/CGNAT addresses on every lookup. Prefers the first
    validated IP for subsequent lookups in the same download context; if a
    CDN rotates that address out of the answer set, re-pins to a new safe IP
    rather than aborting (identity pins break TikTok/Akamai and similar CDNs).

    The getaddrinfo wrapper is installed once process-wide; per-download pin
    state lives in thread-local storage so concurrent ``asyncio.to_thread``
    downloads neither nest wrappers nor leak pins across hosts.
    """
    _ensure_dns_guard_installed()
    existing: dict[str, Any] | None = getattr(_dns_guard_tls, "state", None)
    if existing is not None:
        existing["depth"] = int(existing["depth"]) + 1
        try:
            yield
        finally:
            existing["depth"] = int(existing["depth"]) - 1
        return

    _dns_guard_tls.state = {"pinned_ips": {}, "depth": 1}
    try:
        yield
    finally:
        _dns_guard_tls.state = None
