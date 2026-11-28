"""Analyse the documents a scan found: download each, extract text and metadata, flag leaks.

This runs separately from the crawl (it downloads files, which is heavier) and writes findings
to the database as it goes. It reuses the crawler's Fetcher, so it keeps the same politeness,
size caps and SSRF guard; parsing runs in a worker thread with the hardening in
webatlas.analysis.extract.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx

from webatlas.analysis.extract import MAX_PARSE_BYTES, extract_document
from webatlas.analysis.findings import scan_document
from webatlas.config import CrawlConfig
from webatlas.db import Database
from webatlas.fetcher import Fetcher, FetchError
from webatlas.safety import Resolver, UnsafeTargetError, default_resolver

# Types we have an analyser for (webatlas.analysis.extract). Archives are skipped.
ANALYZABLE = frozenset({"pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub",
                        "doc", "xls", "ppt", "office-legacy", "txt", "csv", "rtf"})

FLUSH_EVERY = 25          # write findings and counters to the DB every N documents
PROGRESS_SLICE = 0.5      # seconds between forced counter flushes
MAX_STORE_CHARS = 60000   # how much extracted text to keep per document for the AI layer


class Analyzer:
    def __init__(self, db: Database, scan_id: str, *, allow_private: bool = False,
                 max_documents: int | None = None, max_duration: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver = default_resolver,
                 on_event: Callable[[dict], None] | None = None):
        self.db = db
        self.scan_id = scan_id
        self.max_documents = max_documents
        self.on_event = on_event or (lambda event: None)
        self.config = CrawlConfig(allow_private_networks=allow_private, per_host_delay=0.3,
                                  max_document_bytes=MAX_PARSE_BYTES, max_duration=max_duration)
        self.transport = transport
        self.resolver = resolver
        self._stop = False
        self.analyzed = 0
        self.findings_count = 0
        self.severity: dict[str, int] = {}
        self._content: list[dict] = []    # extracted text kept for the AI layer
        self._deadline = time.monotonic() + max_duration if max_duration else None

    def request_stop(self) -> None:
        self._stop = True

    def _out_of_time(self) -> bool:
        return self._stop or (self._deadline is not None and time.monotonic() >= self._deadline)

    async def run(self) -> None:
        # A fresh analysis replaces any earlier one for this scan.
        self.db.clear_findings(self.scan_id)
        self.db.clear_ai(self.scan_id)   # stored text/embeddings are stale once we re-analyse
        self.db.set_status(self.scan_id, analysis_status="running", analysis_error="",
                           analyzed_docs=0, findings=0, severity_counts="{}")
        targets = [(i, url, dt) for i, url, dt in self.db.documents_for_analysis(self.scan_id)
                   if dt in ANALYZABLE]
        pending: list[dict] = []
        seen_urls: set[str] = set()
        status = "done"
        last_flush = time.monotonic()

        async with Fetcher(self.config, transport=self.transport, resolver=self.resolver) as fetcher:
            for doc_id, url, doc_type in targets:
                if self._out_of_time():
                    status = "stopped"
                    break
                if self.max_documents is not None and self.analyzed >= self.max_documents:
                    break
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                rows = await self._analyze_one(fetcher, doc_id, url, doc_type)
                pending.extend(rows)
                self.analyzed += 1
                if len(pending) >= 200 or self.analyzed % FLUSH_EVERY == 0 or \
                        time.monotonic() - last_flush >= PROGRESS_SLICE:
                    self._flush(pending)
                    pending = []
                    last_flush = time.monotonic()

        self._flush(pending)
        self.db.set_status(self.scan_id, analysis_status=status, analyzed_docs=self.analyzed,
                           findings=self.findings_count, severity_counts=_json(self.severity))
        self.on_event({"type": "analysis_done", "analyzed": self.analyzed,
                       "findings": self.findings_count, "status": status})

    async def _analyze_one(self, fetcher: Fetcher, doc_id: int, url: str, doc_type: str) -> list[dict]:
        try:
            response = await fetcher.get(url, max_bytes=self.config.max_document_bytes)
        except (FetchError, UnsafeTargetError) as exc:
            self.on_event({"type": "analysis_skip", "url": url, "reason": str(exc)})
            return []
        if not response.ok or not response.body:
            self.on_event({"type": "analysis_skip", "url": url, "reason": f"HTTP {response.status}"})
            return []
        # Parsing is CPU-bound and runs on untrusted bytes: do it off the event loop.
        extracted = await asyncio.to_thread(extract_document, response.body, doc_type)
        findings = await asyncio.to_thread(scan_document, extracted)
        if extracted.text.strip():
            title = extracted.metadata.get("title", "")
            self._content.append({"scan_id": self.scan_id, "document_id": doc_id, "url": url,
                                  "title": title, "doc_type": doc_type,
                                  "text": extracted.text[:MAX_STORE_CHARS]})
        rows = []
        for f in findings:
            rows.append({"scan_id": self.scan_id, "document_id": doc_id, "url": url, "doc_type": doc_type,
                         "rule": f.rule, "severity": f.severity, "title": f.title,
                         "location": f.location, "detail": f.detail, "count": f.count})
            self.severity[f.severity] = self.severity.get(f.severity, 0) + 1
        self.findings_count += len(rows)
        if findings:
            self.on_event({"type": "analysis_finding", "url": url, "count": len(rows),
                           "top": findings[0].severity})
        return rows

    def _flush(self, rows: list[dict]) -> None:
        self.db.add_findings(rows)
        if self._content:
            self.db.store_content(self._content)
            self._content = []
        self.db.set_status(self.scan_id, analyzed_docs=self.analyzed, findings=self.findings_count,
                           severity_counts=_json(self.severity))


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj)
