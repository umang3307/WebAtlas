"""HTTP fetching with politeness, retries, redirect checks and size limits.

Every request goes through Fetcher.request(), which:
  1. checks the target host is public (SSRF guard), on every redirect hop
  2. waits so one host never gets more than one request per `per_host_delay`
  3. retries timeouts, 429 and 5xx with exponential backoff (honours Retry-After)
  4. stops reading the body once it passes a size limit
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from webatlas.config import CrawlConfig
from webatlas.safety import Resolver, check_host, default_resolver
from webatlas.urls import host_of, is_http_url

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str                     # URL that was requested
    final_url: str               # URL after redirects
    status: int
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    body: bytes = b""
    truncated: bool = False      # body was cut off at the size limit

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        charset = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"'")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class FetchError(Exception):
    pass


class HostThrottle:
    """Makes sure requests to the same host are at least `delay` seconds apart."""

    def __init__(self, delay: float):
        self.delay = delay
        self._next_slot: dict[str, float] = {}
        self._overrides: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def set_delay(self, host: str, delay: float) -> None:
        # Used for robots.txt Crawl-delay (capped at 30 s); never faster than the default.
        self._overrides[host] = max(min(delay, 30.0), self.delay)

    async def wait(self, host: str) -> None:
        delay = self._overrides.get(host, self.delay)
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + delay
        if slot > now:
            await asyncio.sleep(slot - now)


def _retry_after_seconds(headers: httpx.Headers, default: float) -> float:
    value = headers.get("retry-after")
    if not value:
        return default
    if value.isdigit():
        return min(float(value), 60.0)
    try:
        return max(0.0, min(parsedate_to_datetime(value).timestamp() - time.time(), 60.0))
    except (TypeError, ValueError):
        return default


class Fetcher:
    def __init__(
        self,
        config: CrawlConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
    ):
        self.config = config
        self.resolver = resolver
        self.throttle = HostThrottle(config.per_host_delay)
        self._checked_hosts: set[str] = set()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": config.user_agent, "Accept": "*/*"},
            timeout=config.timeout,
            follow_redirects=False,   # we follow redirects ourselves to SSRF-check each hop
            transport=transport,
        )

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def guard(self, url: str) -> None:
        """Raise unless `url` is http(s) and its host is public (skipped with allow_private_networks)."""
        if not is_http_url(url):
            raise FetchError(f"not an http(s) URL: {url}")
        if self.config.allow_private_networks:
            return
        host = host_of(url)
        if host not in self._checked_hosts:
            # Note: this checks DNS before the request; a hostile DNS server could still
            # change its answer in between ("DNS rebinding"). Fine for a crawler, but the
            # Phase 2 API should also run behind a network egress policy.
            await check_host(host, self.resolver)
            self._checked_hosts.add(host)

    async def request(self, method: str, url: str, max_bytes: int | None = None) -> FetchResult:
        """Send `method` to `url`, following redirects and retrying transient failures."""
        max_bytes = self.config.max_page_bytes if max_bytes is None else max_bytes
        current = url
        for _hop in range(self.config.max_redirects + 1):
            await self.guard(current)
            result = await self._send_with_retries(method, current, max_bytes)
            if result.status in (301, 302, 303, 307, 308) and "location" in result.headers:
                current = urljoin(current, result.headers["location"])
                if result.status == 303:
                    method = "GET"
                continue
            result.url = url
            result.final_url = current
            return result
        raise FetchError(f"too many redirects starting at {url}")

    async def _send_with_retries(self, method: str, url: str, max_bytes: int) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            await self.throttle.wait(host_of(url))
            try:
                result = await self._send_once(method, url, max_bytes)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 10))
                continue
            if result.status in RETRY_STATUSES and attempt < self.config.retries:
                await asyncio.sleep(_retry_after_seconds(result.headers, min(2**attempt, 10)))
                continue
            return result
        raise FetchError(f"{type(last_error).__name__}: {last_error}" if last_error else "request failed")

    async def _send_once(self, method: str, url: str, max_bytes: int) -> FetchResult:
        async with self._client.stream(method, url) as response:
            body = bytearray()
            truncated = False
            if method != "HEAD":
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        del body[max_bytes:]
                        truncated = True
                        break
            return FetchResult(
                url=url, final_url=url, status=response.status_code,
                headers=response.headers, body=bytes(body), truncated=truncated,
            )

    async def get(self, url: str, max_bytes: int | None = None) -> FetchResult:
        return await self.request("GET", url, max_bytes)

    async def head(self, url: str) -> FetchResult:
        return await self.request("HEAD", url)

    async def peek(self, url: str, n_bytes: int = 4096) -> FetchResult:
        """GET only the first few KB — enough to read headers and the file's magic bytes."""
        return await self.request("GET", url, max_bytes=n_bytes)
