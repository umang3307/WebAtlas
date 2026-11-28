"""Plain data records produced by a crawl."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Link:
    url: str
    text: str = ""


@dataclass
class Page:
    url: str
    depth: int
    status: int
    title: str = ""
    links_found: int = 0
    documents_found: int = 0
    rendered: bool = False   # True if the JavaScript fallback was used
    found_on: str = ""       # page that linked here ("" for seeds and sitemap entries)


@dataclass
class Document:
    url: str                       # URL as linked (normalized)
    final_url: str                 # URL after redirects
    title: str                     # link text, or the file name
    source_page: str               # page that linked to it ("" if it came from a sitemap)
    doc_type: str                  # pdf, docx, xlsx, ...
    content_type: str = ""
    size_bytes: int | None = None
    last_modified: str = ""
    sha256: str | None = None      # only when download_documents is on
    detected_by: str = ""          # "content-type", "content-disposition", "magic-bytes", "extension"
                                   # or "extension-unverified" (not checked: doc-check budget used up)


@dataclass
class Failure:
    url: str
    reason: str


@dataclass
class CrawlResult:
    """Counts are always kept. The lists are filled only when results stay in memory
    (MemorySink); with a database sink they stay empty and the rows live in the database."""
    seeds: list[str]
    pages: list[Page] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    skipped_by_robots: list[str] = field(default_factory=list)
    pages_count: int = 0
    documents_count: int = 0
    failures_count: int = 0
    robots_skipped_count: int = 0
    sitemap_urls: int = 0
    requests: int = 0              # HTTP requests sent (including retries and robots/sitemaps)
    stopped_early: str = ""        # why the crawl ended before the queue was empty, if it did
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
