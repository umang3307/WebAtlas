"""The crawl loop.

How a crawl works
-----------------
1. Every seed URL is checked (must be public, allowed by robots.txt) and queued.
2. If enabled, the site's sitemap is read and its URLs are queued too.
3. A few worker tasks take items off a priority queue:
     * "page" items are fetched as HTML. Their links are sorted into
         - document candidates (link looks like a file)  -> queued as "doc" items
         - same-site pages within max_depth              -> queued as "page" items
       If a "page" turns out to be a file (e.g. /download.php?id=5 returns a PDF),
       it's recorded as a document instead.
     * "doc" items are checked with a HEAD request (or the first 4 KB) so the
       document is confirmed by its headers or magic bytes, not just its name.
4. The queue is ordered by depth (breadth-first), so pages near the seed are
   crawled first and the page budget is spent on the most relevant pages.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from webatlas.config import CrawlConfig
from webatlas.detect import classify_response, doc_type_from_url, is_html, sha256_of
from webatlas.extract import extract_page
from webatlas.fetcher import Fetcher, FetchError, FetchResult
from webatlas.models import CrawlResult, Document, Failure, Page
from webatlas.render import Renderer, RenderUnavailable
from webatlas.robots import RobotsCache
from webatlas.safety import Resolver, UnsafeTargetError, default_resolver
from webatlas.sitemap import collect_sitemap_urls
from webatlas.urls import file_name, in_scope, normalize_url

EventCallback = Callable[[dict], None]

PAGE, DOC = "page", "doc"


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    kind: str = ""
    url: str = ""
    depth: int = 0
    source_page: str = ""
    link_text: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Crawler:
    def __init__(
        self,
        config: CrawlConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        on_event: EventCallback | None = None,
    ):
        self.config = config or CrawlConfig()
        self.transport = transport
        self.resolver = resolver
        self.on_event = on_event or (lambda event: None)
        self._stop_requested = False
        self.result: CrawlResult | None = None
        self.fetcher: Fetcher | None = None

    def request_stop(self) -> None:
        """Ask a running crawl to finish early. Safe to call from another thread;
        the crawl returns everything found so far."""
        self._stop_requested = True

    # ------------------------------------------------------------------ public

    async def crawl(self, seeds: list[str]) -> CrawlResult:
        cfg = self.config
        normalized = [u for u in (normalize_url(s) for s in seeds) if u]
        if not normalized:
            raise ValueError("no valid http(s) seed URLs given")
        self.root = normalized[0]
        self.result = CrawlResult(seeds=normalized, started_at=_now())
        self._queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._seen: set[str] = set()
        self._doc_keys: set[str] = set()
        self._pages_reserved = 0
        self._renderer: Renderer | None = None
        self._render_ok = cfg.render_js
        self._doc_checks = 0
        self._deadline = time.monotonic() + cfg.max_duration if cfg.max_duration else None

        async with Fetcher(cfg, transport=self.transport, resolver=self.resolver) as fetcher:
            self.fetcher = fetcher
            self.robots = RobotsCache(fetcher, cfg.user_agent, enabled=cfg.respect_robots)

            for seed in normalized:
                self._enqueue(PAGE, seed, depth=0)

            if cfg.use_sitemaps:
                await self._queue_sitemap_urls()

            workers = [asyncio.create_task(self._worker()) for _ in range(max(1, cfg.concurrency))]
            try:
                await self._queue.join()
            finally:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if self._renderer:
                    await self._renderer.close()

        self.result.requests = fetcher.requests_made
        self.result.finished_at = _now()
        self._emit("done", pages=len(self.result.pages), documents=len(self.result.documents),
                   failures=len(self.result.failures))
        return self.result

    # ------------------------------------------------------------------ queue

    def _enqueue(self, kind: str, url: str, depth: int, source_page: str = "", link_text: str = "",
                 from_sitemap: bool = False) -> None:
        key = f"{kind}:{url}"
        if key in self._seen:
            return
        self._seen.add(key)
        # Documents are cheap to check, so they go first. Pages are breadth-first; sitemap
        # pages come after menu links of the same depth.
        priority = -1 if kind == DOC else depth * 2 + (1 if from_sitemap else 0)
        self._queue.put_nowait(_Item(priority, next(self._seq), kind, url, depth, source_page, link_text))

    async def _queue_sitemap_urls(self) -> None:
        try:
            urls = await collect_sitemap_urls(self.root, self.fetcher, self.robots, self.config.max_sitemap_urls)
        except (FetchError, UnsafeTargetError) as exc:
            self._emit("info", message=f"sitemap skipped: {exc}")
            return
        count = 0
        for raw in urls:
            url = normalize_url(raw)
            if not url or not in_scope(url, self.root, self.config.include_subdomains):
                continue
            count += 1
            if doc_type_from_url(url):
                self._enqueue(DOC, url, depth=1)
            elif self.config.max_depth >= 1:
                self._enqueue(PAGE, url, depth=1, from_sitemap=True)
        self.result.sitemap_urls = count
        self._emit("info", message=f"sitemap listed {count} in-scope URLs")

    def _out_of_time(self) -> bool:
        if self._stop_requested:
            if not self.result.stopped_early:
                self.result.stopped_early = "stopped by user"
                self._emit("info", message="stopping: stopped by user")
            return True
        if self._deadline is not None and time.monotonic() >= self._deadline:
            if not self.result.stopped_early:
                self.result.stopped_early = f"time limit ({self.config.max_duration:g} s) reached"
                self._emit("info", message=f"stopping: {self.result.stopped_early}")
            return True
        return False

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if self._out_of_time():
                    continue   # drain the queue quickly; finally: still marks the item done
                if item.kind == PAGE:
                    await self._process_page(item)
                else:
                    await self._process_doc(item)
            except (FetchError, UnsafeTargetError) as exc:
                self._fail(item.url, str(exc))
            except Exception as exc:  # never let one bad page kill a worker
                self._fail(item.url, f"unexpected {type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ pages

    async def _process_page(self, item: _Item) -> None:
        cfg = self.config
        if self._pages_reserved >= cfg.max_pages:
            return
        if not await self.robots.allowed(item.url):
            self.result.skipped_by_robots.append(item.url)
            self._emit("skipped", url=item.url, reason="robots.txt")
            return
        if self._pages_reserved >= cfg.max_pages:   # re-check: other workers ran during the await
            return
        self._pages_reserved += 1
        self._emit("fetching", url=item.url, depth=item.depth,
                   pages_done=len(self.result.pages), max_pages=cfg.max_pages)
        response = await self.fetcher.get(item.url)

        if not response.ok:
            self._fail(item.url, f"HTTP {response.status}")
            return

        # Some "pages" are really files, e.g. /download.php?id=5 -> PDF.
        doc = classify_response(response.final_url, response.content_type,
                                response.headers.get("content-disposition", ""), response.body)
        if doc:
            self._pages_reserved -= 1   # a file doesn't use up the page budget
            await self._record_document(item, response, *doc)
            return
        if not is_html(response.content_type, response.body):
            return   # images, JSON, etc.

        final = normalize_url(response.final_url) or item.url
        if final != item.url:
            if f"{PAGE}:{final}" in self._seen:
                # Redirected to a page that is already queued or crawled - don't do it twice.
                self._pages_reserved -= 1
                return
            self._seen.add(f"{PAGE}:{final}")
            if not in_scope(final, self.root, cfg.include_subdomains):
                self._fail(item.url, f"redirected off-site to {final}")
                return

        title, links = extract_page(response.text(), final)
        rendered = False
        if self._render_ok and len(links) < cfg.render_min_links:
            rendered_links = await self._render(final)
            if rendered_links is not None:
                title, links = rendered_links
                rendered = True

        docs_here = 0
        for link in links:
            if doc_type_from_url(link.url):
                if cfg.include_offsite_documents or in_scope(link.url, self.root, cfg.include_subdomains):
                    self._enqueue(DOC, link.url, item.depth + 1, source_page=final, link_text=link.text)
                    docs_here += 1
            elif item.depth + 1 <= cfg.max_depth and in_scope(link.url, self.root, cfg.include_subdomains):
                self._enqueue(PAGE, link.url, item.depth + 1, source_page=final, link_text=link.text)

        page = Page(url=final, depth=item.depth, status=response.status, title=title,
                    links_found=len(links), documents_found=docs_here, rendered=rendered,
                    found_on=item.source_page)
        self.result.pages.append(page)
        self._emit("page", url=final, depth=item.depth, title=title, links=len(links), found_on=item.source_page,
                   documents=docs_here, pages_done=len(self.result.pages), max_pages=cfg.max_pages)

    async def _render(self, url: str):
        try:
            if self._renderer is None:
                self._renderer = Renderer(self.config.user_agent, guard=self.fetcher.guard)
            html = await self._renderer.render(url)
        except RenderUnavailable as exc:
            self._render_ok = False
            self._emit("info", message=str(exc))
            return None
        except Exception as exc:
            self._emit("info", message=f"render failed for {url}: {exc}")
            return None
        return extract_page(html, url)

    # ------------------------------------------------------------------ documents

    async def _process_doc(self, item: _Item) -> None:
        cfg = self.config
        if len(self.result.documents) >= cfg.max_documents:
            return
        if not await self.robots.allowed(item.url):
            self.result.skipped_by_robots.append(item.url)
            self._emit("skipped", url=item.url, reason="robots.txt")
            return

        if self._doc_checks >= cfg.max_doc_checks:
            # Budget for confirming documents is used up: list it from the link alone.
            self._record_unverified(item)
            return
        self._doc_checks += 1

        if cfg.download_documents:
            response = await self.fetcher.get(item.url, max_bytes=cfg.max_document_bytes)
        else:
            response = await self.fetcher.head(item.url)
            # Many servers answer HEAD badly (405, or a generic content type). Then read
            # just the first 4 KB so we can check the magic bytes.
            needs_peek = (not response.ok) or classify_response(
                response.final_url, response.content_type,
                response.headers.get("content-disposition", ""), b"") is None
            if needs_peek:
                response = await self.fetcher.peek(item.url)

        if not response.ok:
            self._fail(item.url, f"HTTP {response.status}")
            return
        doc = classify_response(response.final_url, response.content_type,
                                response.headers.get("content-disposition", ""), response.body)
        if not doc:
            kind = response.content_type or "unknown type"
            self._fail(item.url, f"linked like a document but served as {kind}")
            return
        await self._record_document(item, response, *doc)

    async def _record_document(self, item: _Item, response: FetchResult, doc_type: str, detected_by: str) -> None:
        cfg = self.config
        if len(self.result.documents) >= cfg.max_documents:
            return
        final = normalize_url(response.final_url) or item.url

        digest = None
        if cfg.download_documents and response.body and not response.truncated:
            digest = sha256_of(response.body)

        # The same file can be linked under several URLs; dedupe on URL and on content hash.
        keys = {f"url:{item.url}", f"url:{final}"} | ({f"sha:{digest}"} if digest else set())
        if keys & self._doc_keys:
            return
        self._doc_keys |= keys

        size = response.headers.get("content-length")
        if size is None and not response.truncated and cfg.download_documents:
            size = len(response.body)
        document = Document(
            url=item.url,
            final_url=final,
            title=item.link_text or file_name(final),
            source_page=item.source_page,
            doc_type=doc_type,
            content_type=response.content_type,
            size_bytes=int(size) if size is not None and str(size).isdigit() else None,
            last_modified=response.headers.get("last-modified", ""),
            sha256=digest,
            detected_by=detected_by,
        )
        self.result.documents.append(document)
        self._emit("document", url=final, title=document.title, doc_type=doc_type,
                   detected_by=detected_by, documents_done=len(self.result.documents))

    def _record_unverified(self, item: _Item) -> None:
        key = f"url:{item.url}"
        if key in self._doc_keys or len(self.result.documents) >= self.config.max_documents:
            return
        self._doc_keys.add(key)
        document = Document(url=item.url, final_url=item.url, title=item.link_text or file_name(item.url),
                            source_page=item.source_page, doc_type=doc_type_from_url(item.url) or "unknown",
                            detected_by="extension-unverified")
        self.result.documents.append(document)
        self._emit("document", url=item.url, title=document.title, doc_type=document.doc_type,
                   detected_by=document.detected_by, documents_done=len(self.result.documents))

    # ------------------------------------------------------------------ helpers

    def _fail(self, url: str, reason: str) -> None:
        self.result.failures.append(Failure(url=url, reason=reason))
        self._emit("failure", url=url, reason=reason)

    def _emit(self, event_type: str, **data) -> None:
        try:
            self.on_event({"type": event_type, **data})
        except Exception:
            pass  # a broken progress callback must not stop the crawl


async def crawl(seeds: list[str], config: CrawlConfig | None = None, **kwargs) -> CrawlResult:
    """Convenience wrapper: `await crawl(["https://example.edu"], CrawlConfig(max_depth=2))`."""
    return await Crawler(config, **kwargs).crawl(seeds)


async def discover_sections(
    root_url: str,
    config: CrawlConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
) -> tuple[list[str], list[str]]:
    """Read only the homepage and return (same-site page links, document links).
    Used by the web UI to let the user pick which sections to crawl."""
    cfg = config or CrawlConfig()
    root = normalize_url(root_url)
    if not root:
        raise ValueError(f"not a valid http(s) URL: {root_url}")
    async with Fetcher(cfg, transport=transport, resolver=resolver) as fetcher:
        robots = RobotsCache(fetcher, cfg.user_agent, enabled=cfg.respect_robots)
        if not await robots.allowed(root):
            raise FetchError(f"robots.txt does not allow crawling {root}")
        response = await fetcher.get(root)
        if not response.ok:
            raise FetchError(f"HTTP {response.status} for {root}")
        final = normalize_url(response.final_url) or root
        _title, links = extract_page(response.text(), final)
        sections, docs = [], []
        for link in links:
            if doc_type_from_url(link.url):
                docs.append(link.url)
            elif (in_scope(link.url, final, cfg.include_subdomains)
                  and link.url.rstrip("/") != final.rstrip("/")
                  and await robots.allowed(link.url)):   # don't offer sections robots.txt forbids
                sections.append(link.url)
    return sorted(set(sections)), sorted(set(docs))
