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

from webatlas.ai import rag
from webatlas.ai.client import LLMClient
from webatlas.ai.indexer import AiIndexer
from webatlas.analyzer import Analyzer
from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.db import Database, DbSink, DocumentRow, FindingRow, IssueRow, PageRow
from webatlas.safety import Resolver, default_resolver

log = logging.getLogger("webatlas.web")

MAX_EVENTS = 4000            # activity feed entries kept per scan
MAX_RUNNING = 3              # scans allowed at the same time
MAX_ANALYSES = 2            # document analyses allowed at the same time
MAX_INDEXES = 2             # AI indexing jobs allowed at the same time
KEEP_FINISHED_IN_MEMORY = 5  # older finished scans are served from the database
DEFAULT_TIME_LIMIT = 600.0   # seconds, when the request doesn't set one
ANALYSIS_TIME_LIMIT = 1800.0  # 30 min default cap on a document analysis
INDEX_TIME_LIMIT = 1800.0    # 30 min default cap on AI indexing
MAX_QUESTION_CHARS = 2000    # cap on a question sent to the AI layer


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


class AnalysisJob:
    """A document analysis running (or just finished) in this process. Counters live in the
    scan row (the analyzer writes them); this holds only status, stop, and a short event feed."""

    def __init__(self, scan_id: str):
        self.id = scan_id
        self.status = "running"
        self.started_mono = time.monotonic()
        self.events: list[dict] = []
        self.events_dropped = 0
        self.analyzer: Analyzer | None = None
        self.thread: threading.Thread | None = None

    def add_event(self, event: dict) -> None:
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            trim = len(self.events) - MAX_EVENTS
            del self.events[:trim]
            self.events_dropped += trim


class AiIndexJob:
    """An AI indexing run (classification + embeddings). Counters live in the scan row;
    this holds status, stop and a short event feed."""

    def __init__(self, scan_id: str):
        self.id = scan_id
        self.status = "running"
        self.started_mono = time.monotonic()
        self.events: list[dict] = []
        self.events_dropped = 0
        self.indexer: AiIndexer | None = None
        self.thread: threading.Thread | None = None

    def add_event(self, event: dict) -> None:
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            trim = len(self.events) - MAX_EVENTS
            del self.events[:trim]
            self.events_dropped += trim


class ScanManager:
    def __init__(self, db: Database, allow_private: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver = default_resolver,
                 llm_client: LLMClient | None = None):
        self.db = db
        self.allow_private = allow_private
        self.transport = transport      # tests inject a fake website here
        self.resolver = resolver
        self.llm_client = llm_client    # tests inject a fake model here; else built from env
        self._llm_lock = threading.Lock()
        self.jobs: dict[str, ScanJob] = {}
        self.analyses: dict[str, AnalysisJob] = {}
        self.indexes: dict[str, AiIndexJob] = {}
        self._lock = threading.Lock()

    def _llm(self) -> LLMClient:
        """The configured model client (from WEBATLAS_LLM_* env), built once and reused.
        When nothing is configured it still returns a client whose .available() is False, so
        callers degrade to the offline path."""
        if self.llm_client is not None:
            return self.llm_client
        with self._llm_lock:
            if self.llm_client is None:
                self.llm_client = LLMClient()
        return self.llm_client

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

    # ---------------------------------------------------------------- analysis

    def analyze(self, scan_id: str) -> AnalysisJob | None:
        with self._lock:
            row = self.db.get_scan(scan_id)
            if row is None:
                return None
            if row["status"] == "running":
                raise RuntimeError("wait for the crawl to finish before analysing its documents")
            existing = self.analyses.get(scan_id)
            if existing and existing.status == "running":
                return existing
            running = sum(1 for a in self.analyses.values() if a.status == "running")
            if running >= MAX_ANALYSES:
                raise TooManyScans(f"{running} analyses are already running; wait for one to finish")
            self._evict_finished_analyses()
            job = AnalysisJob(scan_id)
            job.analyzer = Analyzer(self.db, scan_id, allow_private=self.allow_private,
                                    max_duration=ANALYSIS_TIME_LIMIT, transport=self.transport,
                                    resolver=self.resolver, on_event=job.add_event)
            self.analyses[scan_id] = job
        job.thread = threading.Thread(target=self._run_analysis, args=(job,), daemon=True,
                                      name=f"analyze-{scan_id}")
        job.thread.start()
        return job

    def _run_analysis(self, job: AnalysisJob) -> None:
        try:
            asyncio.run(job.analyzer.run())
            job.status = "done"
        except Exception as exc:
            job.status = "error"
            log.exception("analysis of %s failed", job.id)
            try:
                self.db.set_status(job.id, analysis_status="error",
                                   analysis_error=f"{type(exc).__name__}: {exc}")
            except Exception:
                log.exception("could not record analysis failure for %s", job.id)

    def stop_analysis(self, scan_id: str) -> bool:
        job = self.analyses.get(scan_id)
        if not job or job.status != "running":
            return False
        job.analyzer.request_stop()
        return True

    def _evict_finished_analyses(self) -> None:
        done = [a for a in self.analyses.values() if a.status != "running"]
        done.sort(key=lambda a: a.started_mono, reverse=True)
        for old in done[KEEP_FINISHED_IN_MEMORY:]:
            if old.thread is None or not old.thread.is_alive():
                self.analyses.pop(old.id, None)

    # ---------------------------------------------------------------- AI layer

    def build_index(self, scan_id: str) -> AiIndexJob | None:
        with self._lock:
            row = self.db.get_scan(scan_id)
            if row is None:
                return None
            if row["status"] == "running":
                raise RuntimeError("wait for the crawl to finish before building the AI index")
            if not self.db.has_content(scan_id):
                raise RuntimeError("analyse the documents first; the AI index is built from their text")
            existing = self.indexes.get(scan_id)
            if existing and existing.status == "running":
                return existing
            running = sum(1 for a in self.indexes.values() if a.status == "running")
            if running >= MAX_INDEXES:
                raise TooManyScans(f"{running} indexing jobs are already running; wait for one to finish")
            self._evict_finished_indexes()
            job = AiIndexJob(scan_id)
            job.indexer = AiIndexer(self.db, scan_id, client=self._llm(),
                                    max_duration=INDEX_TIME_LIMIT, on_event=job.add_event)
            self.indexes[scan_id] = job
        job.thread = threading.Thread(target=self._run_index, args=(job,), daemon=True,
                                      name=f"index-{scan_id}")
        job.thread.start()
        return job

    def _run_index(self, job: AiIndexJob) -> None:
        try:
            job.indexer.run()      # synchronous
            job.status = "done"
        except Exception as exc:
            job.status = "error"
            log.exception("AI indexing of %s failed", job.id)
            try:
                self.db.set_status(job.id, ai_status="error",
                                   ai_error=f"{type(exc).__name__}: {exc}")
            except Exception:
                log.exception("could not record AI index failure for %s", job.id)

    def stop_index(self, scan_id: str) -> bool:
        job = self.indexes.get(scan_id)
        if not job or job.status != "running":
            return False
        job.indexer.request_stop()
        return True

    def _evict_finished_indexes(self) -> None:
        done = [a for a in self.indexes.values() if a.status != "running"]
        done.sort(key=lambda a: a.started_mono, reverse=True)
        for old in done[KEEP_FINISHED_IN_MEMORY:]:
            if old.thread is None or not old.thread.is_alive():
                self.indexes.pop(old.id, None)

    def ask(self, scan_id: str, question: str) -> rag.Answer:
        question = (question or "")[:MAX_QUESTION_CHARS]
        return rag.answer(self.db, scan_id, question, client=self._llm())

    def ai_info(self) -> dict:
        """What the model layer looks like right now — for the console's AI panel."""
        client = self._llm()
        info = client.info()
        info["available"] = client.available()
        return info

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
        findings = self.db.rows_after(FindingRow, scan_id, cursors.get("findings", 0))
        for rows in (docs, pages, issues, findings):
            for r in rows:
                r.pop("scan_id", None)

        # Passive security findings are low-volume and deduplicated, so send the whole set.
        security = self.db.all_security(scan_id)
        for r in security:
            r.pop("scan_id", None)
        security_counts = self.db.security_severity_counts(scan_id)

        # Analysis fields always come from the scan row (the analyzer writes them there).
        arow = self.db.get_scan(scan_id)
        analysis = {
            "status": arow["analysis_status"] if arow else "none",
            "analyzed": arow["analyzed_docs"] if arow else 0,
            "findings": arow["findings"] if arow else 0,
            "severity": arow["severity_counts"] if arow else {},
            "error": arow["analysis_error"] if arow else "",
            "running": scan_id in self.analyses and self.analyses[scan_id].status == "running",
        }
        return {
            "id": scan_id, "target": target, "status": status, "error": error,
            "started_at": started_at, "settings": settings, "max_pages": max_pages,
            "resumable": status in ("interrupted", "stopped"),
            "analyzable": status in ("done", "stopped") and analysis["status"] != "running",
            "ai": {
                "status": arow["ai_status"] if arow else "none",
                "indexed": arow["indexed_docs"] if arow else 0,
                "chunks": arow["chunks"] if arow else 0,
                "embed_kind": arow["embed_kind"] if arow else "",
                "error": arow["ai_error"] if arow else "",
                "running": scan_id in self.indexes and self.indexes[scan_id].status == "running",
            },
            # The index is built from analysed text, so it needs analysis to have produced some.
            "indexable": analysis["analyzed"] > 0 and analysis["status"] != "running",
            "summary": summary, "analysis": analysis,
            "security": security, "security_counts": security_counts,
            "events": events, "events_total": events_total,
            "documents": docs, "pages": pages, "issues": issues, "findings": findings,
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
        analysis = self.analyses.get(scan_id)
        if analysis and analysis.status == "running":
            raise RuntimeError("stop the analysis before deleting the scan")
        index = self.indexes.get(scan_id)
        if index and index.status == "running":
            raise RuntimeError("stop the AI indexing before deleting the scan")
        self.jobs.pop(scan_id, None)
        self.analyses.pop(scan_id, None)
        self.indexes.pop(scan_id, None)
        return self.db.delete_scan(scan_id)
