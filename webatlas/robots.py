"""robots.txt handling: one cached parser per host."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from protego import Protego

from webatlas.fetcher import Fetcher, FetchError

ROBOTS_MAX_BYTES = 512 * 1024


class RobotsCache:
    def __init__(self, fetcher: Fetcher, user_agent: str, enabled: bool = True):
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: dict[str, Protego | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        except FetchError:
            return None  # unreachable robots.txt: treat as "everything allowed"
        if result.status in (401, 403):
            # Google's convention: a forbidden robots.txt means "crawl nothing".
            return Protego.parse("User-agent: *\nDisallow: /")
        if not result.ok:
            return None   # 404 etc.: no rules
        parser = Protego.parse(result.text())
        delay = parser.crawl_delay(self.user_agent)
        if delay:
            self.fetcher.throttle.set_delay(urlsplit(origin).hostname or "", float(delay))
        return parser

    async def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parser = await self._parser_for(url)
        return True if parser is None else parser.can_fetch(url, self.user_agent)

    async def sitemaps(self, url: str) -> list[str]:
        parser = await self._parser_for(url)
        return list(parser.sitemaps) if parser else []
