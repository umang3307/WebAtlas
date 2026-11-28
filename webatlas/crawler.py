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
from datetime import UTC, datetime

import httpx

from webatlas.config import CrawlConfig
from webatlas.detect import classify_response, doc_type_from_url, is_html, sha256_of
from webatlas.extract import extract_page
from webatlas.fetcher import Fetcher, FetchError, FetchResult
from webatlas.models import CrawlResult, Document, Failure, Page
from webatlas.render import Renderer, RenderUnavailable
from webatlas.robots import RobotsCache
from webatlas.safety import Resolver, UnsafeTargetError, default_resolver
from webatlas.sinks import MemorySink, QueueItem, ResultSink, ResumeState
from webatlas.sitemap import collect_sitemap_urls
from webatlas.urls import file_name, in_scope, looks_like_trap, normalize_url

EventCallback = Callable[[dict], None]

PAGE, DOC = "page", "doc"


def _under(count: int, limit: int | float | None) -> bool:
    """True while `count` is below `limit`; a limit of None means no limit."""
    return limit is None or count < limit


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Crawler:
    def __init__(
        self,
        config: CrawlConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        on_event: EventCallback | None = None,
        sink: ResultSink | None = None,
    ):
        """`sink` receives every result; by default they're kept in memory on the CrawlResult."""
        self.config = config or CrawlConfig()
        self.transport = transport
        self.resolver = resolver
        self.on_event = on_event or (lambda event: None)
        self._sink_override = sink
        self._stop_requested = False
        self.result: CrawlResult | None = None
        self.fetcher: Fetcher | None = None

    def request_stop(self) -> None:
        """Ask a running crawl to finish early. Safe to call from another thread;
        the crawl returns everything found so far."""
        self._stop_requested = True

    # ------------------------------------------------------------------ public

    async def crawl(self, seeds: list[str], resume: ResumeState | None = None) -> CrawlResult:
        """Crawl from `seeds`. With `resume`, continue an interrupted crawl: already-seen URLs
        are not fetched again and only the work that was still queued is done."""
        cfg = self.config
        normalized = [u for u in (normalize_url(s) for s in seeds) if u]
        if not normalized:
            raise ValueError("no valid http(s) seed URLs given")
        self.root = normalized[0]
        self.result = CrawlResult(seeds=normalized, started_at=_now())
        self.sink: ResultSink = self._sink_override or MemorySink(self.result)
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._seen: set[str] = set()
        self._doc_keys: set[str] = set()
        self._pages_reserved = 0
        self._renderer: Renderer | None = None
        self._render_ok = cfg.render_js
        self._doc_checks = 0
        self._requests_before = 0
        self._sitemap_done = False
        self._deadline = time.monotonic() + cfg.max_duration if cfg.max_duration else None

        if resume is not None:
            self._seen = set(resume.seen)
            self._doc_keys = set(resume.doc_keys)
            self._pages_reserved = resume.pages_done
            self._doc_checks = resume.doc_checks
            self._requests_before = resume.requests
            self._sitemap_done = resume.sitemap_done
            r = self.result
            r.pages_count, r.documents_count = resume.pages_done, resume.documents_done
            r.failures_count, r.robots_skipped_count = resume.failures, resume.robots_skipped
            r.sitemap_urls = resume.sitemap_urls
            self._sitemap_count = resume.sitemap_urls

        async with Fetcher(cfg, transport=self.transport, resolver=self.resolver) as fetcher:
            self.fetcher = fetcher
            self.robots = RobotsCache(fetcher, cfg.user_agent, enabled=cfg.respect_robots,
                                      on_note=lambda message: self._emit("info", message=message))

            if resume is None:
                for seed in normalized:
                    self._enqueue(PAGE, seed, depth=0)
            else:
                for item in resume.pending:   # already in _seen; put straight back on the queue
                    item.seq = next(self._seq)
                    self._queue.put_nowait(item)

            # Crawling starts right away; the sitemap is read alongside and feeds the same queue.
            workers = [asyncio.create_task(self._worker()) for _ in range(max(1, cfg.concurrency))]
            tasks = list(workers)
            try:
                if cfg.use_sitemaps and not self._sitemap_done:
                    sitemap_task = asyncio.create_task(self._queue_sitemap_urls())
                    tasks.append(sitemap_task)
                    await self._until_done_or_stopped(sitemap_task)
                if not self._out_of_time():
                    join_task = asyncio.create_task(self._queue.join())
                    tasks.append(join_task)
                    await self._until_done_or_stopped(join_task)
            finally:
                # On Stop or a time limit this also cancels requests still in flight,
                # so stopping takes effect within a fraction of a second.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self._renderer:
                    await self._renderer.close()

        self.result.requests = self._requests_before + fetcher.requests_made
        self.result.finished_at = _now()
        self._emit("done", pages=self.result.pages_count, documents=self.result.documents_count,
                   failures=self.result.failures_count)
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
        item = QueueItem(priority, next(self._seq), kind, url, depth, source_page, link_text, from_sitemap)
        self.sink.enqueued(item)
        self._queue.put_nowait(item)

    async def _until_done_or_stopped(self, task: asyncio.Task) -> None:
        """Wait for `task`, checking four times a second whether to stop early."""
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            if not task.done() and self._out_of_time():
                return

    async def _queue_sitemap_urls(self) -> None:
        self._sitemap_count = getattr(self, "_sitemap_count", 0)

        def take(urls: list[str]) -> None:
            for raw in urls:
                url = normalize_url(raw)
                if not url or not in_scope(url, self.root, self.config.include_subdomains):
                    continue
                self._sitemap_count += 1
                if doc_type_from_url(url):
                    self._enqueue(DOC, url, depth=1)
                elif _under(0, self.config.max_depth):   # depth 1 allowed
                    self._enqueue(PAGE, url, depth=1, from_sitemap=True)
            self.result.sitemap_urls = self._sitemap_count

        try:
            await collect_sitemap_urls(self.root, self.fetcher, self.robots, self.config.max_sitemap_urls,
                                       on_batch=take, should_stop=self._out_of_time)
        except (FetchError, UnsafeTargetError) as exc:
            self._emit("info", message=f"sitemap skipped: {exc}")
        except Exception as exc:   # a broken sitemap must never stop the crawl
            self._emit("info", message=f"sitemap skipped: unexpected {type(exc).__name__}: {exc}")
        else:
            self._emit("info", message=f"sitemap listed {self._sitemap_count} in-scope URLs")
        if not self._out_of_time():   # finished (or failed for good), not cut short: don't redo on resume
            self._sitemap_done = True
            self.sink.sitemap_done(self._sitemap_count)

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
                    continue   # leave it unprocessed: a resumed crawl will pick it up
                try:
                    if item.kind == PAGE:
                        await self._process_page(item)
                    else:
                        await self._process_doc(item)
                except (FetchError, UnsafeTargetError) as exc:
                    self._fail(item.url, str(exc))
                except Exception as exc:  # never let one bad page kill a worker
                    self._fail(item.url, f"unexpected {type(exc).__name__}: {exc}")
                # Reached only when the item was really handled (not cancelled by Stop).
                self.sink.processed(item)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ pages

    async def _process_page(self, item: QueueItem) -> None:
        cfg = self.config
        if not _under(self._pages_reserved, cfg.max_pages):
            return
        if not await self.robots.allowed(item.url):
            self._skip_robots(item.url)
            return
        if not _under(self._pages_reserved, cfg.max_pages):   # re-check: other workers ran during the await
            return
        self._pages_reserved += 1
        self._emit("fetching", url=item.url, depth=item.depth,
                   pages_done=self.result.pages_count, max_pages=cfg.max_pages)
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

        # Parsing is CPU work: run it in a thread so other requests, progress and Stop stay live.
        title, links = await asyncio.to_thread(extract_page, response.body, final, response.charset)
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
            elif _under(item.depth, cfg.max_depth) and in_scope(link.url, self.root, cfg.include_subdomains):
                if looks_like_trap(link.url):
                    self._skip_trap(link.url)
                    continue
                self._enqueue(PAGE, link.url, item.depth + 1, source_page=final, link_text=link.text)

        page = Page(url=final, depth=item.depth, status=response.status, title=title,
                    links_found=len(links), documents_found=docs_here, rendered=rendered,
                    found_on=item.source_page)
        self.result.pages_count += 1
        self.sink.page(page)
        self._emit("page", url=final, depth=item.depth, title=title, links=len(links), found_on=item.source_page,
                   documents=docs_here, pages_done=self.result.pages_count, max_pages=cfg.max_pages)

    async def _render(self, url: str):
        try:
            if self._renderer is None:
                self._renderer = Renderer(self.config.user_agent, guard=self.fetcher.guard)
            html = await self._renderer.render(url)
            return await asyncio.to_thread(extract_page, html, url)
        except RenderUnavailable as exc:
            self._render_ok = False
            self._emit("info", message=str(exc))
            return None
        except Exception as exc:
            self._emit("info", message=f"render failed for {url}: {exc}")
            return None

    # ------------------------------------------------------------------ documents

    async def _process_doc(self, item: QueueItem) -> None:
        cfg = self.config
        if not _under(self.result.documents_count, cfg.max_documents):
            return
        if not await self.robots.allowed(item.url):
            self._skip_robots(item.url)
            return

        if not _under(self._doc_checks, cfg.max_doc_checks):
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

    async def _record_document(self, item: QueueItem, response: FetchResult, doc_type: str, detected_by: str) -> None:
        cfg = self.config
        if not _under(self.result.documents_count, cfg.max_documents):
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
        self._add_document(document)

    def _record_unverified(self, item: QueueItem) -> None:
        key = f"url:{item.url}"
        if key in self._doc_keys or not _under(self.result.documents_count, self.config.max_documents):
            return
        self._doc_keys.add(key)
        document = Document(url=item.url, final_url=item.url, title=item.link_text or file_name(item.url),
                            source_page=item.source_page, doc_type=doc_type_from_url(item.url) or "unknown",
                            detected_by="extension-unverified")
        self._add_document(document)

    # ------------------------------------------------------------------ helpers

    def _add_document(self, document: Document) -> None:
        self.result.documents_count += 1
        self.sink.document(document)
        self._emit("document", url=document.final_url, title=document.title, doc_type=document.doc_type,
                   detected_by=document.detected_by, documents_done=self.result.documents_count)

    def _skip_robots(self, url: str) -> None:
        reason = self.robots.why_blocked(url)
        self.result.robots_skipped_count += 1
        self.sink.robots_skip(url, reason)
        self._emit("skipped", url=url, reason=reason)

    def _skip_trap(self, url: str) -> None:
        key = f"trap:{url}"
        if key in self._seen:
            return
        self._seen.add(key)
        self._fail(url, "skipped: looks like a crawler trap (URL too long or path repeats itself)", kind="trap")

    def _fail(self, url: str, reason: str, kind: str = "failure") -> None:
        self.result.failures_count += 1
        self.sink.failure(Failure(url=url, reason=reason), kind)
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
        _title, links = await asyncio.to_thread(extract_page, response.body, final, response.charset)
        sections, docs = [], []
        for link in links:
            if doc_type_from_url(link.url):
                docs.append(link.url)
            elif (in_scope(link.url, final, cfg.include_subdomains)
                  and link.url.rstrip("/") != final.rstrip("/")
                  and await robots.allowed(link.url)):   # don't offer sections robots.txt forbids
                sections.append(link.url)
    return sorted(set(sections)), sorted(set(docs))
