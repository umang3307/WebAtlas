"""SSRF protection.

WebAtlas runs as a web app: anyone who can reach it can type a URL. Without
this check, a user could make the server fetch http://127.0.0.1:6379/,
http://169.254.169.254/ (cloud metadata with credentials) or hosts on the
internal network. Every request, including every redirect hop, is checked here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

Resolver = Callable[[str], Awaitable[list[str]]]


class UnsafeTargetError(Exception):
    """Raised when a URL resolves to a private, loopback or otherwise internal address."""


_NAT64 = ipaddress.ip_network("64:ff9b::/96")   # well-known NAT64 prefix (RFC 6052)


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv6 forms that carry an IPv4 address inside them and reach it on the network."""
    if addr.ipv4_mapped:                          # ::ffff:a.b.c.d
        return addr.ipv4_mapped
    if addr in _NAT64:                            # 64:ff9b::a.b.c.d, translated by a NAT64 gateway
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if int(addr) >> 32 == 0 and int(addr) > 1:    # ::a.b.c.d (deprecated "IPv4-compatible")
        return ipaddress.IPv4Address(int(addr))
    return None


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_ipv4(addr)
        if inner is not None:
            addr = inner
    return addr.is_global and not addr.is_multicast


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def check_host(host: str, resolver: Resolver = default_resolver) -> None:
    """Raise UnsafeTargetError unless every address `host` resolves to is public."""
    if not host:
        raise UnsafeTargetError("empty host")
    try:
        # A literal IP needs no DNS lookup.
        ips = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        try:
            ips = await resolver(host)
        except OSError as exc:
            raise UnsafeTargetError(f"could not resolve {host}: {exc}") from exc
    if not ips:
        raise UnsafeTargetError(f"{host} did not resolve to any address")
    bad = [ip for ip in ips if not is_public_ip(ip)]
    if bad:
        raise UnsafeTargetError(f"{host} resolves to non-public address {bad[0]}")
