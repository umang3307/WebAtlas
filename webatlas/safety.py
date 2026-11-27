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


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
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
