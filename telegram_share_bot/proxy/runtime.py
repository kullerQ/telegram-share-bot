"""Process-wide lifecycle for optional HTTP proxy routing."""

from __future__ import annotations

import os
import threading

from telegram_share_bot.proxy.gateway import ProxyGateway
from telegram_share_bot.proxy.resolver import DEFAULT_DNS_URL, Address, ProtectedResolver

_no_proxy_lock = threading.RLock()
_no_proxy_active_runtimes = 0
_no_proxy_snapshot: dict[str, str] | None = None


def _acquire_no_proxy_policy() -> None:
    """Keep subprocesses on the explicitly configured gateway while it is active."""
    global _no_proxy_active_runtimes, _no_proxy_snapshot
    with _no_proxy_lock:
        if _no_proxy_active_runtimes == 0:
            _no_proxy_snapshot = {
                key: value for key, value in os.environ.items() if key.lower() == "no_proxy"
            }
            for key in tuple(_no_proxy_snapshot):
                os.environ.pop(key, None)
        _no_proxy_active_runtimes += 1


def _release_no_proxy_policy() -> None:
    global _no_proxy_active_runtimes, _no_proxy_snapshot
    with _no_proxy_lock:
        if _no_proxy_active_runtimes == 0:
            return
        _no_proxy_active_runtimes -= 1
        if _no_proxy_active_runtimes:
            return
        snapshot = _no_proxy_snapshot or {}
        for key, value in snapshot.items():
            if key not in os.environ:
                os.environ[key] = value
        _no_proxy_snapshot = None


class ProxyRuntime:
    """Proxy routing policy and optional protected-DNS forwarding gateway.

    While any runtime is active, ``NO_PROXY`` is temporarily removed so child
    processes cannot bypass the application's explicit local proxy gateway.
    """

    def __init__(
        self,
        proxy_url: str,
        dns_protection: bool = True,
        dns_url: str = DEFAULT_DNS_URL,
    ) -> None:
        from telegram_share_bot.config.proxy import parse_dns_url, parse_proxy_url

        validated_proxy_url = parse_proxy_url(proxy_url)
        if validated_proxy_url is None:
            raise ValueError("An upstream proxy URL is required")
        self.proxy_url = validated_proxy_url
        self.dns_protection = dns_protection
        self.dns_url = parse_dns_url(dns_url)
        self._resolver: ProtectedResolver | None = None
        self._gateway: ProxyGateway | None = None
        self._closed = False
        self._no_proxy_policy_active = False
        self._lock = threading.Lock()
        _acquire_no_proxy_policy()
        self._no_proxy_policy_active = True
        try:
            if dns_protection:
                self._resolver = ProtectedResolver(self.proxy_url, self.dns_url)
            self._gateway = ProxyGateway(self.proxy_url, self._resolver)
        except BaseException:
            try:
                if self._resolver is not None:
                    self._resolver.close()
            finally:
                _release_no_proxy_policy()
                self._no_proxy_policy_active = False
            raise

    @property
    def client_proxy_url(self) -> str:
        if self._gateway is not None:
            return self._gateway.client_url
        return self.proxy_url

    def resolve_addresses(self, host: str) -> tuple[Address, ...]:
        if not self.dns_protection or self._resolver is None:
            raise RuntimeError("Protected DNS is disabled")
        return self._resolver.resolve(host)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._gateway is not None:
                self._gateway.close()
        finally:
            try:
                if self._resolver is not None:
                    self._resolver.close()
            finally:
                if self._no_proxy_policy_active:
                    _release_no_proxy_policy()
                    self._no_proxy_policy_active = False


_runtime: ProxyRuntime | None = None
_runtime_lock = threading.RLock()


def get_proxy_runtime() -> ProxyRuntime | None:
    with _runtime_lock:
        return _runtime


def configure_proxy(
    proxy_url: str | None,
    dns_protection: bool = True,
    dns_url: str = DEFAULT_DNS_URL,
) -> ProxyRuntime | None:
    """Install a proxy runtime, reusing identical configuration when possible."""
    global _runtime
    from telegram_share_bot.config.proxy import parse_dns_url, parse_proxy_url

    proxy_url = parse_proxy_url(proxy_url)
    if not proxy_url:
        with _runtime_lock:
            previous = _runtime
            _runtime = None
        if previous is not None:
            previous.close()
        return None

    dns_url = parse_dns_url(dns_url)
    with _runtime_lock:
        if (
            _runtime is not None
            and _runtime.proxy_url == proxy_url
            and _runtime.dns_protection == dns_protection
            and _runtime.dns_url == dns_url
        ):
            return _runtime
        new_runtime = ProxyRuntime(proxy_url, dns_protection, dns_url)
        previous = _runtime
        _runtime = new_runtime
        if previous is not None:
            previous.close()
        return new_runtime


def close_proxy(runtime: ProxyRuntime | None = None) -> None:
    """Close a runtime and clear the global only when it is the active instance."""
    global _runtime
    with _runtime_lock:
        target = runtime if runtime is not None else _runtime
        if target is _runtime:
            _runtime = None
    if target is not None:
        target.close()
