"""Where a crawl's results go.

The crawler reports everything it finds to a *sink*:

  MemorySink  keeps results in a CrawlResult in memory (CLI, library use, tests)
  DbSink      writes them to the database while the crawl runs (web console; see webatlas.db)

Sinks also hear about the crawl *frontier* (every URL queued, and when each one has been
handled). A sink that stores the frontier can later resume a crawl exactly where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from webatlas.models import CrawlResult, Document, Failure, Page


@dataclass(order=True)
class QueueItem:
    """One URL waiting in the crawl queue. Ordered by (priority, seq)."""
    priority: int
    seq: int
    kind: str = field(default="", compare=False)          # "page" or "doc"
    url: str = field(default="", compare=False)
    depth: int = field(default=0, compare=False)
    source_page: str = field(default="", compare=False)
    link_text: str = field(default="", compare=False)
    from_sitemap: bool = field(default=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.url}"


@dataclass
class ResumeState:
    """Everything a crawler needs to continue an interrupted crawl."""
    pending: list[QueueItem]            # queued but never finished
    seen: set[str]                      # "page:<url>", "doc:<url>", "trap:<url>" keys already queued
    doc_keys: set[str]                  # "url:<url>" / "sha:<hash>" of documents already recorded
    pages_done: int = 0
    documents_done: int = 0
    doc_checks: int = 0
    failures: int = 0
    robots_skipped: int = 0
    requests: int = 0
    sitemap_urls: int = 0
    sitemap_done: bool = False


class ResultSink(Protocol):
    def page(self, page: Page) -> None: ...
    def document(self, document: Document) -> None: ...
    def failure(self, failure: Failure, kind: str = "failure") -> None: ...
    def robots_skip(self, url: str, reason: str) -> None: ...
    def enqueued(self, item: QueueItem) -> None: ...
    def processed(self, item: QueueItem) -> None: ...
    def sitemap_done(self, urls: int) -> None: ...


class MemorySink:
    """Keeps every result in the CrawlResult's lists. Frontier events are ignored."""

    def __init__(self, result: CrawlResult):
        self.result = result

    def page(self, page: Page) -> None:
        self.result.pages.append(page)

    def document(self, document: Document) -> None:
        self.result.documents.append(document)

    def failure(self, failure: Failure, kind: str = "failure") -> None:
        self.result.failures.append(failure)

    def robots_skip(self, url: str, reason: str) -> None:
        self.result.skipped_by_robots.append(url)

    def enqueued(self, item: QueueItem) -> None:
        pass

    def processed(self, item: QueueItem) -> None:
        pass

    def sitemap_done(self, urls: int) -> None:
        pass
