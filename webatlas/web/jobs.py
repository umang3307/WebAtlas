"""Background scans for the web interface.

Each scan runs the async crawler in its own thread with its own event loop, writing results
straight to the database as it goes (webatlas.db.DbSink). The job keeps only counters and a
short activity feed in memory, so memory stays flat however large the scan is.

The browser polls with cursors. For documents, pages and issues a cursor is the id of the last
row it has; the server returns rows with a greater id. For the activity feed it's a count.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.db import Database, DbSink, DocumentRow, IssueRow, PageRow
from webatlas.safety import Resolver, default_resolver

log = logging.getLogger("webatlas.web")

MAX_EVENTS = 4000            # activity feed entries kept per scan
MAX_RUNNING = 3              # scans allowed at the same time
KEEP_FINISHED_IN_MEMORY = 5  # older finished scans are served from the database
DEFAULT_TIME_LIMIT = 600.0   # seconds, when the request doesn't set one


class TooManyScans(Exception):
    pass


SETTING_LIMITS: dict[str, tuple[type, float, float]] = {
    "max_depth": (int, 0, 100),
    "max_pages": (int, 1, 10_000_000),
    "max_duration": (float, 10, 30 * 24 * 3600),
    "max_doc_checks": (int, 0, 10_000_000),
    "concurrency": (int, 1, 16),
    "per_host_delay": (float, 0, 10),
}
# Limits a user may switch off entirely by sending null. Concurrency and delay always apply:
# they protect the target site, not the user's time.
LIFTABLE = ("max_depth", "max_pages", "max_duration", "max_doc_checks")
BOOL_SETTINGS = ("use_sitemaps", "include_subdomains", "include_offsite_documents", "render_js",
                 "download_documents")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_settings(raw: dict) -> dict:
    """Keep only known settings, coerce types and clamp to safe ranges.
    A liftable limit sent as null means "no limit"; a missing time limit gets the default."""
    out: dict = {}
    for key, (kind, low, high) in SETTING_LIMITS.items():
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            if key in LIFTABLE:
                out[key] = None
            continue
        try:
            out[key] = kind(min(max(kind(value), low), high))
        except (TypeError, ValueError, OverflowError):
            continue
    for key in BOOL_SETTINGS:
        if key in raw:
            out[key] = bool(raw[key])
    if "max_duration" not in out:
        out["max_duration"] = DEFAULT_TIME_LIMIT
    return out


class ScanJob:
    """A scan running (or just finished) in this process."""

    def __init__(self, scan_id: str, target: str, seeds: list[str], settings: dict,
                 started_at: str, prior_elapsed: float = 0.0):
        self.id = scan_id
        self.target = target
        self.seeds = seeds
        self.settings = settings
        self.started_at = started_at
        self.prior_elapsed = prior_elapsed     # crawl time from earlier runs (resume)
        self.status = "running"
        self.error = ""
        self.started_mono = time.monotonic()
        self.finished_mono: float | None = None
        self.events: list[dict] = []
        self.events_dropped = 0
        self.crawler: Crawler | None = None
        self.thread: threading.Thread | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_mono or time.monotonic()
        return self.prior_elapsed + (end - self.started_mono)

    def add_event(self, event: dict) -> None:
        if event["type"] == "fetching":
            return   # too chatty for the feed; "page" follows
        self.events.append({**event, "t": round(self.elapsed, 1)})
        if len(self.events) > MAX_EVENTS:
            trim = len(self.events) - MAX_EVENTS
            del self.events[:trim]
            self.events_dropped += trim

    def counters(self) -> dict:
        """Live counts for the scan row, read from the crawler as it runs."""
        r = self.crawler.result if self.crawler else None
        if r is None:
            return {}
        requests = r.requests
        if self.crawler and self.crawler.fetcher is not None and self.status == "running":
            requests = (getattr(self.crawler, "_requests_before", 0)) + self.crawler.fetcher.requests_made
        return {"pages": r.pages_count, "documents": r.documents_count, "failures": r.failures_count,
                "robots_skipped": r.robots_skipped_count, "requests": requests,
                "doc_checks": getattr(self.crawler, "_doc_checks", 0), "sitemap_urls": r.sitemap_urls,
                "elapsed": round(self.elapsed, 1)}


class ScanManager:
    def __init__(self, db: Database, allow_private: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver = default_resolver):
        self.db = db
        self.allow_private = allow_private
        self.transport = transport      # tests inject a fake website here
        self.resolver = resolver
        self.jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def _evict_finished(self) -> None:
        finished = [j for j in self.jobs.values() if j.status != "running"]
        finished.sort(key=lambda j: j.started_mono, reverse=True)
        for old in finished[KEEP_FINISHED_IN_MEMORY:]:
            if old.thread is None or not old.thread.is_alive():
                self.jobs.pop(old.id, None)

    def _running_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def start(self, target: str, seeds: list[str], settings: dict) -> ScanJob:
        with self._lock:
            if self._running_count() >= MAX_RUNNING:
                raise TooManyScans(f"{self._running_count()} scans are already running; "
                                   "stop one or wait for it to finish")
            self._evict_finished()
            scan_id = uuid.uuid4().hex[:12]
            job = ScanJob(scan_id, target, seeds or [target], settings, _now())
            self.db.create_scan(scan_id, target, job.seeds, settings, job.started_at)
            self.jobs[scan_id] = job
        self._launch(job, resume=False)
        return job

    def resume(self, scan_id: str) -> ScanJob | None:
        """Continue a scan that was interrupted (server stopped) or stopped by the user."""
        with self._lock:
            row = self.db.get_scan(scan_id)
            if row is None or row["status"] not in ("interrupted", "stopped"):
                return None
            if scan_id in self.jobs and self.jobs[scan_id].status == "running":
                return self.jobs[scan_id]
            if self._running_count() >= MAX_RUNNING:
                raise TooManyScans("too many scans are already running")
            self._evict_finished()
            job = ScanJob(scan_id, row["target"], row["seeds"], row["settings"], row["started_at"],
                          prior_elapsed=row["elapsed"])
            self.db.set_status(scan_id, status="running", error="", stopped_early="", finished_at=None)
            self.jobs[scan_id] = job
        self._launch(job, resume=True)
        return job

    def _launch(self, job: ScanJob, resume: bool) -> None:
        config = CrawlConfig(allow_private_networks=self.allow_private, **job.settings)
        sink = DbSink(self.db, job.id)
        sink.counters = {}
        if resume:
            # Keep running totals (type counts) continuous across the resume.
            sink._doc_types = self.db.doc_type_counts(job.id)
        job.crawler = Crawler(config, transport=self.transport, resolver=self.resolver,
                              on_event=job.add_event, sink=sink)
        job.thread = threading.Thread(target=self._run, args=(job, sink, resume), daemon=True,
                                      name=f"scan-{job.id}")
        job.thread.start()

    def _run(self, job: ScanJob, sink: DbSink, resume: bool) -> None:
        error = ""
        try:
            resume_state = self.db.resume_state(job.id) if resume else None
            # Keep the scan row's counters current on every flush, so an interrupted scan
            # resumes from accurate numbers.
            sink.counters = {}
            self._attach_counters(job, sink)
            result = asyncio.run(job.crawler.crawl(job.seeds, resume=resume_state))
            status = "stopped" if result.stopped_early == "stopped by user" else "done"
            stopped_early = result.stopped_early
        except Exception as exc:
            status, error, stopped_early = "error", f"{type(exc).__name__}: {exc}", ""
            log.exception("scan %s failed", job.id)
        job.finished_mono = time.monotonic()
        # Flush everything and record the final status in one go. The scan shows as finished
        # only once its rows and counters are saved, so a reload can't catch it half-written.
        try:
            final = {**job.counters(), "status": status, "error": error, "stopped_early": stopped_early,
                     "finished_at": _now()}
            sink.flush(**final)
        except Exception as exc:
            error = (error + "; " if error else "") + f"results could not be saved: {exc}"
            log.exception("saving scan %s failed", job.id)
            try:
                self.db.set_status(job.id, status=status, error=error, finished_at=_now())
            except Exception:
                log.exception("could not even record the failure for scan %s", job.id)
        job.error = error
        job.status = status

    def _attach_counters(self, job: ScanJob, sink: DbSink) -> None:
        # DbSink.flush merges whatever is in sink.counters; refresh it from the live crawler
        # each time a flush happens by wrapping flush. Cheap and keeps the row current.
        original_maybe = sink._maybe_flush

        def refresh_then_flush() -> None:
            sink.counters = job.counters()
            original_maybe()

        sink._maybe_flush = refresh_then_flush

    def stop(self, scan_id: str) -> bool:
        job = self.jobs.get(scan_id)
        if not job or job.status != "running":
            return False
        job.crawler.request_stop()
        return True

    # ---------------------------------------------------------------- reads

    def snapshot(self, scan_id: str, cursors: dict[str, int]) -> dict | None:
        job = self.jobs.get(scan_id)
        if job is not None and job.crawler is not None and job.crawler.result is not None:
            counts = job.counters()
            status, error = job.status, job.error
            stopped_early = job.crawler.result.stopped_early
            doc_types = job.crawler.sink._doc_types if hasattr(job.crawler, "sink") else {}
            max_pages = job.crawler.config.max_pages
            settings = {**job.settings, "seeds": job.seeds}
            target, started_at = job.target, job.started_at
            ev_from = max(cursors.get("events", 0) - job.events_dropped, 0)
            events, events_total = job.events[ev_from:], job.events_dropped + len(job.events)
        else:
            row = self.db.get_scan(scan_id)
            if row is None:
                return None
            counts = {k: row[k] for k in ("pages", "documents", "failures", "robots_skipped",
                                          "requests", "elapsed", "sitemap_urls")}
            status, error, stopped_early = row["status"], row["error"], row["stopped_early"]
            doc_types, max_pages = row["doc_types"], row["settings"].get("max_pages", CrawlConfig.max_pages)
            settings, target, started_at = row["settings"], row["target"], row["started_at"]
            events, events_total = [], 0

        summary = {k: counts.get(k, 0) for k in ("pages", "documents", "failures", "robots_skipped",
                                                 "requests", "sitemap_urls")}
        summary["elapsed"] = counts.get("elapsed", 0)
        summary["doc_types"] = dict(sorted(doc_types.items(), key=lambda kv: kv[1], reverse=True))
        summary["stopped_early"] = stopped_early

        docs = self.db.rows_after(DocumentRow, scan_id, cursors.get("documents", 0))
        pages = self.db.rows_after(PageRow, scan_id, cursors.get("pages", 0))
        issues = self.db.rows_after(IssueRow, scan_id, cursors.get("issues", 0))
        for rows in (docs, pages, issues):
            for r in rows:
                r.pop("scan_id", None)
        return {
            "id": scan_id, "target": target, "status": status, "error": error,
            "started_at": started_at, "settings": settings, "max_pages": max_pages,
            "resumable": status in ("interrupted", "stopped"),
            "summary": summary, "events": events, "events_total": events_total,
            "documents": docs, "pages": pages, "issues": issues,
        }

    def list_scans(self, limit: int = 50) -> list[dict]:
        rows = self.db.list_scans(limit)
        out = []
        for r in rows:
            out.append({"id": r["id"], "target": r["target"], "status": r["status"],
                        "started_at": r["started_at"],
                        "summary": {"documents": r["documents"], "pages": r["pages"]}})
        return out

    def delete(self, scan_id: str) -> bool:
        job = self.jobs.get(scan_id)
        if job and job.status == "running":
            raise RuntimeError("stop the scan before deleting it")
        self.jobs.pop(scan_id, None)
        return self.db.delete_scan(scan_id)
