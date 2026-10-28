"""Resolve public destination addresses through the configured HTTP proxy."""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from telegram_share_bot.media.security import _is_safe_ip

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
DEFAULT_DNS_URL = "https://cloudflare-dns.com/dns-query"
_CACHE_LIMIT = 512
_CACHE_MAX_TTL = 60


class ProtectedResolver:
    """Small bounded A/AAAA cache backed by DNS-over-HTTPS via an HTTP proxy."""

    def __init__(self, proxy_url: str, dns_url: str = DEFAULT_DNS_URL) -> None:
        parsed = urlsplit(dns_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("DNS JSON endpoint must be an HTTPS URL without credentials")
        self._dns_url = dns_url
        self._client = httpx.Client(
            proxy=proxy_url,
            trust_env=False,
            timeout=httpx.Timeout(10.0, connect=10.0),
            headers={"accept": "application/dns-json"},
            follow_redirects=False,
        )
        self._cache: OrderedDict[str, tuple[float, tuple[Address, ...]]] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
        self._client.close()

    def resolve(self, hostname: str) -> tuple[Address, ...]:
        host = hostname.strip().lower().rstrip(".")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not _is_safe_ip(literal):
                raise ValueError("DNS answer contains an unsafe address")
            return (literal,)
        from telegram_share_bot.media.security import _is_public_hostname_syntax

        if not _is_public_hostname_syntax(host):
            raise ValueError("Hostname is malformed or internal")
        now = time.monotonic()
        with self._lock:
            if self._closed:
                raise RuntimeError("Protected DNS resolver is closed")
            cached = self._cache.get(host)
            if cached is not None:
                expires, cached_addresses = cached
                if expires > now:
                    self._cache.move_to_end(host)
                    return cached_addresses
                del self._cache[host]

        addresses: list[Address] = []
        ttls: list[int] = []
        for record_type in (1, 28):
            answer, ttl = self._query(host, record_type)
            for address in answer:
                if not _is_safe_ip(address):
                    raise ValueError("DNS answer contains an unsafe address")
                if address not in addresses:
                    addresses.append(address)
            if answer:
                ttls.append(ttl)
        if not addresses:
            raise ValueError("DNS returned no usable addresses")

        ttl_seconds = min(_CACHE_MAX_TTL, min(ttls))
        if ttl_seconds > 0:
            with self._lock:
                if not self._closed:
                    self._cache[host] = (time.monotonic() + ttl_seconds, tuple(addresses))
                    self._cache.move_to_end(host)
                    while len(self._cache) > _CACHE_LIMIT:
                        self._cache.popitem(last=False)
        return tuple(addresses)

    def _query(self, host: str, record_type: int) -> tuple[list[Address], int]:
        with self._client.stream(
            "GET",
            self._dns_url,
            params={"name": host, "type": record_type},
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=16 * 1024):
                body.extend(chunk)
                if len(body) > 64 * 1024:
                    raise ValueError("DNS provider response is too large")
        try:
            payload = json.loads(body)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("DNS provider returned malformed JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("DNS provider returned an invalid response")
        if payload.get("TC") not in (None, False, 0):
            raise ValueError("DNS provider returned a truncated response")
        status = payload.get("Status")
        if isinstance(status, bool) or not isinstance(status, int) or status != 0:
            raise ValueError("DNS provider returned an error status")
        answers = payload.get("Answer", [])
        if answers is None:
            answers = []
        if not isinstance(answers, list):
            raise ValueError("DNS provider returned malformed answers")

        expected_version = 4 if record_type == 1 else 6
        found: list[Address] = []
        ttls: list[int] = []
        for record in answers:
            if not isinstance(record, Mapping):
                raise ValueError("DNS provider returned a malformed record")
            kind, value, ttl = record.get("type"), record.get("data"), record.get("TTL")
            if isinstance(kind, bool) or not isinstance(kind, int):
                raise ValueError("DNS provider returned a malformed record type")
            # CNAME and unrelated records may be present in the same answer section.
            if kind != record_type:
                continue
            if not isinstance(value, str) or isinstance(ttl, bool) or not isinstance(ttl, int):
                raise ValueError("DNS provider returned a malformed address record")
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError("DNS provider returned a malformed IP address") from exc
            if address.version != expected_version:
                raise ValueError("DNS provider returned the wrong address family")
            found.append(address)
            ttls.append(max(0, ttl))
        return found, min(ttls) if ttls else _CACHE_MAX_TTL
