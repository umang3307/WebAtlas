"""Background scans for the web interface.

Each scan runs the async crawler in its own thread with its own event loop.
The browser polls for progress with cursors ("give me everything after item N"),
so each poll only carries what's new.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.models import CrawlResult
from webatlas.safety import Resolver, default_resolver
from webatlas.storage import ScanStore

MAX_EVENTS = 4000          # activity feed entries kept per scan
MAX_RUNNING = 3            # scans allowed at the same time
KEEP_FINISHED_IN_MEMORY = 5  # older finished scans are served from the database
DEFAULT_TIME_LIMIT = 600.0   # seconds, when the request doesn't set one


class TooManyScans(Exception):
    pass

# Settings the interface may change, with their allowed ranges.
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def summarize(result: CrawlResult, elapsed: float) -> dict:
    return {
        "pages": len(result.pages),
        "documents": len(result.documents),
        "failures": len(result.failures),
        "robots_skipped": len(result.skipped_by_robots),
        "requests": result.requests,
        "sitemap_urls": result.sitemap_urls,
        "elapsed": round(elapsed, 1),
        "doc_types": dict(Counter(d.doc_type for d in result.documents).most_common()),
        "stopped_early": result.stopped_early,
    }


@dataclass
class ScanJob:
    id: str
    target: str
    seeds: list[str]
    settings: dict
    status: str = "running"            # running | done | stopped | error
    error: str = ""
    started_at: str = field(default_factory=_now)
    started_mono: float = field(default_factory=time.monotonic)
    finished_mono: float | None = None
    events: list[dict] = field(default_factory=list)
    events_dropped: int = 0            # events trimmed from the front of the list
    issues: list[dict] = field(default_factory=list)   # failures and robots skips, in order
    crawler: Crawler | None = None
    thread: threading.Thread | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_mono or time.monotonic()
        return end - self.started_mono

    def add_event(self, event: dict) -> None:
        if event["type"] == "fetching":
            return   # too chatty for the feed; "page" follows
        if event["type"] == "failure":
            self.issues.append({"url": event["url"], "reason": event["reason"], "kind": "failure"})
        elif event["type"] == "skipped":
            self.issues.append({"url": event["url"], "reason": "Blocked by robots.txt", "kind": "robots"})
        event = {**event, "t": round(self.elapsed, 1)}
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            trim = len(self.events) - MAX_EVENTS
            del self.events[:trim]
            self.events_dropped += trim


class ScanManager:
    def __init__(self, store: ScanStore, allow_private: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver = default_resolver):
        self.store = store
        self.allow_private = allow_private
        self.transport = transport      # tests inject a fake website here
        self.resolver = resolver
        self.jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def _evict_finished(self) -> None:
        """Keep memory bounded: finished scans beyond the most recent few are dropped from
        memory (they're already saved to the database and are served from there)."""
        finished = [j for j in self.jobs.values() if j.status != "running"]
        finished.sort(key=lambda j: j.started_mono, reverse=True)
        for old in finished[KEEP_FINISHED_IN_MEMORY:]:
            if old.thread is None or not old.thread.is_alive():
                self.jobs.pop(old.id, None)

    def start(self, target: str, seeds: list[str], settings: dict) -> ScanJob:
        with self._lock:
            running = sum(1 for j in self.jobs.values() if j.status == "running")
            if running >= MAX_RUNNING:
                raise TooManyScans(f"{running} scans are already running; stop one or wait for it to finish")
            self._evict_finished()
        job = ScanJob(id=uuid.uuid4().hex[:12], target=target, seeds=seeds or [target], settings=settings)
        config = CrawlConfig(allow_private_networks=self.allow_private, **settings)
        job.crawler = Crawler(config, transport=self.transport, resolver=self.resolver, on_event=job.add_event)
        self.store.create(job.id, target, {**settings, "seeds": job.seeds}, job.started_at)
        self.jobs[job.id] = job
        job.thread = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"scan-{job.id}")
        job.thread.start()
        return job

    def _run(self, job: ScanJob) -> None:
        result_dict = None
        try:
            result = asyncio.run(job.crawler.crawl(job.seeds))
            job.status = "stopped" if result.stopped_early == "stopped by user" else "done"
            result_dict = result.to_dict()
        except Exception as exc:
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
        job.finished_mono = time.monotonic()
        summary = summarize(job.crawler.result, job.elapsed) if job.crawler.result else {}
        self.store.finish(job.id, job.status, summary, result_dict, job.error or None, _now())

    def stop(self, scan_id: str) -> bool:
        job = self.jobs.get(scan_id)
        if not job or job.status != "running":
            return False
        job.crawler.request_stop()
        return True

    def snapshot(self, scan_id: str, cursors: dict[str, int]) -> dict | None:
        """Everything the interface needs for one poll. `cursors` say how many events,
        documents, pages and issues the browser already has."""
        job = self.jobs.get(scan_id)
        if job is None:
            return self._snapshot_from_store(scan_id, cursors)
        result = job.crawler.result
        if result is None:   # crawl thread hasn't initialised yet
            result = CrawlResult(seeds=job.seeds)
        fetcher = job.crawler.fetcher
        summary = summarize(result, job.elapsed)
        if fetcher is not None and job.status == "running":
            summary["requests"] = fetcher.requests_made
        ev_from = max(cursors.get("events", 0) - job.events_dropped, 0)
        return {
            "id": job.id, "target": job.target, "status": job.status, "error": job.error,
            "started_at": job.started_at, "settings": {**job.settings, "seeds": job.seeds},
            "max_pages": job.crawler.config.max_pages,
            "summary": summary,
            "events": job.events[ev_from:],
            "events_total": job.events_dropped + len(job.events),
            **_slices(result, cursors),
            "issues": job.issues[cursors.get("issues", 0):],
        }

    def _snapshot_from_store(self, scan_id: str, cursors: dict[str, int]) -> dict | None:
        row = self.store.get(scan_id)
        if row is None:
            return None
        result = row["result"] or {"documents": [], "pages": [], "failures": [], "skipped_by_robots": []}
        return {
            "id": row["id"], "target": row["target"], "status": row["status"], "error": row["error"] or "",
            "started_at": row["started_at"], "settings": row["settings"],
            "max_pages": row["settings"].get("max_pages", CrawlConfig.max_pages),
            "summary": row["summary"], "events": [], "events_total": 0,
            **_slices_from_dict(result, cursors),
        }


def _issues(failures, skipped) -> list[dict]:
    return [{"url": f["url"], "reason": f["reason"], "kind": "failure"} for f in failures] + \
           [{"url": u, "reason": "Blocked by robots.txt", "kind": "robots"} for u in skipped]


def _slices(result: CrawlResult, cursors: dict[str, int]) -> dict:
    docs = result.documents[cursors.get("documents", 0):]
    pages = result.pages[cursors.get("pages", 0):]
    return {"documents": [asdict(d) for d in docs], "pages": [asdict(p) for p in pages]}


def _slices_from_dict(result: dict, cursors: dict[str, int]) -> dict:
    issues = _issues(result.get("failures", []), result.get("skipped_by_robots", []))
    return {"documents": result.get("documents", [])[cursors.get("documents", 0):],
            "pages": result.get("pages", [])[cursors.get("pages", 0):],
            "issues": issues[cursors.get("issues", 0):]}
