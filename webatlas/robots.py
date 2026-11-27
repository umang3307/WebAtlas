"""robots.txt handling (RFC 9309): one cached parser per host.

What a robots.txt response means, per the standard:
  2xx                      -> follow its rules
  4xx (except 429)         -> "unavailable": no rules, everything may be crawled
  429, 5xx, network error  -> "unreachable": assume everything is disallowed
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urlsplit

from protego import Protego

from webatlas.fetcher import Fetcher, FetchError

ROBOTS_MAX_BYTES = 512 * 1024   # RFC 9309 asks crawlers to read at least 500 KiB
_DISALLOW_ALL = "User-agent: *\nDisallow: /"


class RobotsCache:
    def __init__(self, fetcher: Fetcher, user_agent: str, enabled: bool = True,
                 on_note: Callable[[str], None] | None = None):
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.enabled = enabled
        self.on_note = on_note or (lambda message: None)
        self._parsers: dict[str, Protego | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._why: dict[str, str] = {}   # origin -> why everything there is blocked

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _parser_for(self, url: str) -> Protego | None:
        origin = self._origin(url)
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._parsers:
                self._parsers[origin] = await self._load(origin)
        return self._parsers[origin]

    async def _load(self, origin: str) -> Protego | None:
        try:
            result = await self.fetcher.get(f"{origin}/robots.txt", max_bytes=ROBOTS_MAX_BYTES)
        except FetchError as exc:
            return self._unreachable(origin, f"could not be fetched ({exc})")
        if result.status == 429 or result.status >= 500:
            return self._unreachable(origin, f"returned HTTP {result.status}")
        if not result.ok:
            return None   # 4xx: no robots.txt, so no rules
        parser = Protego.parse(result.text())
        delay = parser.crawl_delay(self.user_agent)
        if delay:
            self.fetcher.throttle.set_delay(urlsplit(origin).hostname or "", float(delay))
        return parser

    def _unreachable(self, origin: str, what: str) -> Protego:
        reason = f"robots.txt {what}; the standard says to crawl nothing on {urlsplit(origin).hostname}"
        self._why[origin] = reason
        self.on_note(reason)
        return Protego.parse(_DISALLOW_ALL)

    async def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parser = await self._parser_for(url)
        return True if parser is None else parser.can_fetch(url, self.user_agent)

    def why_blocked(self, url: str) -> str:
        return self._why.get(self._origin(url), "Blocked by robots.txt")

    async def sitemaps(self, url: str) -> list[str]:
        parser = await self._parser_for(url)
        return list(parser.sitemaps) if parser else []
