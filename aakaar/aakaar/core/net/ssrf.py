"""SSRF-safe outbound HTTP.

Capabilities that reach arbitrary URLs (``api_call``, ``webhook_send``, the
extended HTTP activities) must not be tricked into hitting internal services
or cloud metadata endpoints. This module builds ``httpx`` clients whose
transport resolves every request's host and refuses to connect to
private / loopback / link-local / reserved / multicast addresses.

On the airgapped target there is no public internet anyway, but workflows do
legitimately call services on the local network. A grant can therefore pass an
``allow_hosts`` allowlist (exact hostnames) — those are permitted to resolve to
private addresses; everything else private is blocked by default.

Note: resolution happens at request time, so this mitigates — but does not
fully eliminate — DNS-rebinding (the OS may resolve again at connect). That is
acceptable for v1 given the airgapped deployment.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable

import httpx

# What `ipaddress.ip_address()` actually returns; the `is_private` family of
# properties live on these concrete classes, not on the private `_BaseAddress`.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class SsrfBlocked(Exception):
    """Raised when an outbound request targets a disallowed address."""


def _resolve_ips(host: str) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"could not resolve host {host!r}: {exc}") from exc
    ips: list[IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ips.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return ips


def _ip_is_safe(ip: IPAddress) -> bool:
    """Public, routable addresses only."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_host_allowed(
    host: str, *, allow_hosts: Iterable[str] = (), allow_private: bool = False
) -> None:
    """Raise SsrfBlocked unless every resolved IP for ``host`` is permitted."""
    if not host:
        raise SsrfBlocked("empty host")
    allow = {h.lower() for h in allow_hosts}
    if allow_private or host.lower() in allow:
        return
    # If the host is already an IP literal, classify it directly. This is both
    # more correct (no DNS) and avoids NAT64/DNS64 hosts synthesizing an IPv6
    # for a public IPv4 literal.
    try:
        ips: list[IPAddress] = [ipaddress.ip_address(host)]
    except ValueError:
        ips = _resolve_ips(host)
    for ip in ips:
        if not _ip_is_safe(ip):
            raise SsrfBlocked(
                f"refusing to connect to {host} ({ip}): non-public address"
            )


class _GuardMixin:
    def __init__(
        self,
        inner: httpx.BaseTransport | httpx.AsyncBaseTransport,
        allow_hosts: Iterable[str],
        allow_private: bool,
    ) -> None:
        self._inner = inner
        self._allow_hosts = tuple(allow_hosts)
        self._allow_private = allow_private

    def _check(self, request: httpx.Request) -> None:
        assert_host_allowed(
            request.url.host,
            allow_hosts=self._allow_hosts,
            allow_private=self._allow_private,
        )


class SsrfGuardTransport(_GuardMixin, httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._check(request)
        return self._inner.handle_request(request)  # type: ignore[union-attr]


class SsrfGuardAsyncTransport(_GuardMixin, httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._check(request)
        return await self._inner.handle_async_request(request)  # type: ignore[union-attr]


def build_async_client(
    *,
    allow_hosts: Iterable[str] = (),
    allow_private: bool = False,
    timeout: float = 30.0,
    **kwargs: object,
) -> httpx.AsyncClient:
    """An httpx.AsyncClient that blocks SSRF targets before connecting."""
    inner = httpx.AsyncHTTPTransport()
    transport = SsrfGuardAsyncTransport(inner, allow_hosts, allow_private)
    return httpx.AsyncClient(transport=transport, timeout=timeout, **kwargs)  # type: ignore[arg-type]


def build_sync_client(
    *,
    allow_hosts: Iterable[str] = (),
    allow_private: bool = False,
    timeout: float = 30.0,
    **kwargs: object,
) -> httpx.Client:
    """A blocking httpx.Client that blocks SSRF targets before connecting."""
    inner = httpx.HTTPTransport()
    transport = SsrfGuardTransport(inner, allow_hosts, allow_private)
    return httpx.Client(transport=transport, timeout=timeout, **kwargs)  # type: ignore[arg-type]


__all__ = [
    "SsrfBlocked",
    "SsrfGuardAsyncTransport",
    "SsrfGuardTransport",
    "assert_host_allowed",
    "build_async_client",
    "build_sync_client",
]
