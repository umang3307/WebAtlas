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
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from webatlas.config import CrawlConfig
from webatlas.safety import Resolver, UnsafeTargetError, check_host, default_resolver, is_public_ip
from webatlas.urls import host_of, is_http_url

RETRY_STATUSES = {429, 500, 502, 503, 504}
# Errors worth retrying: the network or server hiccuped. Anything else (bad gzip data, an invalid
# URL, an unsupported scheme) will fail the same way again, so it's reported at once.
RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


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

    @property
    def charset(self) -> str | None:
        """The charset declared in the Content-Type header, if any. When it's missing, HTML
        parsing sniffs the page's own <meta charset> instead of assuming UTF-8."""
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value.strip("\"' "):
                return value.strip("\"' ")
        return None

    def text(self) -> str:
        try:
            return self.body.decode(self.charset or "utf-8", errors="replace")
        except LookupError:   # unknown charset name
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
        self.requests_made = 0
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
            # DNS could change between this lookup and the connection ("DNS rebinding");
            # _check_peer verifies the address actually connected to as well.
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
                try:
                    current = urljoin(current, result.headers["location"])
                except ValueError as exc:   # e.g. "Location: http://[::1"
                    raise FetchError(f"invalid redirect target from {current}: {exc}") from exc
                if result.status == 303:
                    method = "GET"
                continue
            result.url = url
            result.final_url = current
            return result
        raise FetchError(f"too many redirects starting at {url}")

    async def _send_with_retries(self, method: str, url: str, max_bytes: int) -> FetchResult:
        last_error: Exception | None = None
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            final_attempt = attempt == attempts - 1
            await self.throttle.wait(host_of(url))
            try:
                result = await self._send_once(method, url, max_bytes)
            except RETRYABLE_ERRORS as exc:
                last_error = exc
                if not final_attempt:   # no point waiting after the last try
                    await asyncio.sleep(min(2**attempt, 10))
                continue
            except httpx.HTTPError as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            if result.status in RETRY_STATUSES and not final_attempt:
                await asyncio.sleep(_retry_after_seconds(result.headers, min(2**attempt, 10)))
                continue
            return result
        raise FetchError(f"{type(last_error).__name__}: {last_error}" if last_error else "request failed")

    def _uses_proxy(self, url: str) -> bool:
        """Whether httpx will send this request through a proxy from the environment
        (HTTPS_PROXY / HTTP_PROXY / ALL_PROXY, minus NO_PROXY)."""
        proxies = urllib.request.getproxies_environment()
        parts = urlsplit(url)
        if not (proxies.get(parts.scheme) or proxies.get("all")):
            return False
        return not urllib.request.proxy_bypass_environment(parts.hostname or "", proxies)

    def _check_peer(self, url: str, response: httpx.Response) -> None:
        """Second SSRF check, after connecting: the address the socket actually reached must be
        public too. This closes the gap where DNS answers differently for the guard's lookup and
        the real connection ("DNS rebinding"). Skipped through a proxy, which does the connecting."""
        if self.config.allow_private_networks or self._uses_proxy(url):
            return
        stream = response.extensions.get("network_stream")
        if stream is None:   # in-memory test transports have no socket
            return
        peer = stream.get_extra_info("server_addr")
        if peer and not is_public_ip(peer[0]):
            raise UnsafeTargetError(
                f"{host_of(url)} connected to non-public address {peer[0]} (its DNS answer changed)")

    async def _send_once(self, method: str, url: str, max_bytes: int) -> FetchResult:
        self.requests_made += 1
        async with self._client.stream(method, url) as response:
            self._check_peer(url, response)   # before reading a single byte of the body
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
