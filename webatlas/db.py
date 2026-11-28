"""Scan storage: SQLAlchemy models, a database sink for live crawls, and resume support.

Results are written *while* a crawl runs, in small batches, so:
  - memory stays flat however large the scan gets (the crawler keeps only counters),
  - nothing found is lost if the process stops,
  - an interrupted scan can be resumed from its saved frontier.

SQLite is the default (a single file, nothing to install). The models are plain SQLAlchemy,
so a server database could be used later by changing the URL.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from webatlas.models import Document, Failure, Page
from webatlas.sinks import QueueItem, ResumeState

SCHEMA_VERSION = 2
DEFAULT_DB = os.environ.get("WEBATLAS_DB", "webatlas.db")


class Base(DeclarativeBase):
    pass


class Scan(Base):
    __tablename__ = "scans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target: Mapped[str] = mapped_column(Text)
    seeds: Mapped[str] = mapped_column(Text, default="[]")        # JSON list
    settings: Mapped[str] = mapped_column(Text, default="{}")     # JSON object
    status: Mapped[str] = mapped_column(String(16))               # running|done|stopped|error|interrupted
    error: Mapped[str] = mapped_column(Text, default="")
    stopped_early: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[str] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    elapsed: Mapped[float] = mapped_column(Float, default=0.0)    # seconds of crawling, across resumes
    pages: Mapped[int] = mapped_column(Integer, default=0)
    documents: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    robots_skipped: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    doc_checks: Mapped[int] = mapped_column(Integer, default=0)
    sitemap_urls: Mapped[int] = mapped_column(Integer, default=0)
    sitemap_done: Mapped[bool] = mapped_column(Boolean, default=False)
    doc_types: Mapped[str] = mapped_column(Text, default="{}")    # JSON {type: count}
    # Document analysis (Phase 3), run separately from the crawl.
    analysis_status: Mapped[str] = mapped_column(String(16), default="none")  # none|running|done|stopped|error
    analysis_error: Mapped[str] = mapped_column(Text, default="")
    analyzed_docs: Mapped[int] = mapped_column(Integer, default=0)
    findings: Mapped[int] = mapped_column(Integer, default=0)
    severity_counts: Mapped[str] = mapped_column(Text, default="{}")   # JSON {severity: count}


class PageRow(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer)
    status: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, default="")
    links_found: Mapped[int] = mapped_column(Integer, default=0)
    documents_found: Mapped[int] = mapped_column(Integer, default=0)
    rendered: Mapped[bool] = mapped_column(Boolean, default=False)
    found_on: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (Index("ix_pages_scan", "scan_id", "id"),)


class DocumentRow(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    source_page: Mapped[str] = mapped_column(Text, default="")
    doc_type: Mapped[str] = mapped_column(String(32), default="")
    content_type: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_modified: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detected_by: Mapped[str] = mapped_column(String(32), default="")
    __table_args__ = (Index("ix_documents_scan", "scan_id", "id"),)


class IssueRow(Base):
    __tablename__ = "issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))                 # failure | robots | trap
    __table_args__ = (Index("ix_issues_scan", "scan_id", "id"),)


class FindingRow(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    document_id: Mapped[int] = mapped_column(Integer)      # DocumentRow.id this came from
    url: Mapped[str] = mapped_column(Text)                 # document's final_url
    doc_type: Mapped[str] = mapped_column(String(32), default="")
    rule: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(String(48), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    count: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (Index("ix_findings_scan", "scan_id", "id"),
                      Index("ix_findings_sev", "scan_id", "severity"))


class FrontierRow(Base):
    """Every URL a crawl queued, and whether it was handled. Used only to resume."""
    __tablename__ = "frontier"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer)
    source_page: Mapped[str] = mapped_column(Text, default="")
    link_text: Mapped[str] = mapped_column(Text, default="")
    from_sitemap: Mapped[bool] = mapped_column(Boolean, default=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("scan_id", "kind", "url", name="uq_frontier_item"),
                      Index("ix_frontier_pending", "scan_id", "done"))


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def _row(model, obj) -> dict:
    data = asdict(obj)
    return {k: v for k, v in data.items() if k in model.__table__.columns}


class Database:
    def __init__(self, url_or_path: str | Path = DEFAULT_DB):
        url = str(url_or_path)
        if "://" not in url:
            url = f"sqlite:///{url}"
        self.url = url
        self.engine: Engine = create_engine(url, connect_args={"timeout": 15} if url.startswith("sqlite") else {})
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")      # readers don't block the writer
                cur.execute("PRAGMA foreign_keys=ON")       # so deleting a scan deletes its rows
                cur.execute("PRAGMA synchronous=NORMAL")    # safe with WAL, much faster
                cur.close()
        self._migrate()

    # ---------------------------------------------------------------- schema

    def _migrate(self) -> None:
        insp = inspect(self.engine)
        tables = set(insp.get_table_names())
        legacy = "scans" in tables and "meta" not in tables and \
            "result" in {c["name"] for c in insp.get_columns("scans")}
        legacy_rows = []
        if legacy:
            # v2.0 console stored each scan as one JSON blob. Read it, rebuild in the new tables.
            with self.engine.begin() as conn:
                legacy_rows = [dict(r._mapping) for r in conn.execute(text("SELECT * FROM scans"))]
                conn.execute(text("ALTER TABLE scans RENAME TO scans_v1_backup"))
        Base.metadata.create_all(self.engine)   # creates any missing tables (not missing columns)
        self._add_missing_columns()
        with self.engine.begin() as conn:
            conn.execute(insert(Meta).prefix_with("OR REPLACE"),
                         {"key": "schema_version", "value": str(SCHEMA_VERSION)})
            # Scans that were running when the process stopped can be resumed.
            conn.execute(update(Scan).where(Scan.status == "running").values(status="interrupted"))
        for row in legacy_rows:
            self._import_legacy(row)
        if legacy:
            with self.engine.begin() as conn:
                conn.execute(text("DROP TABLE scans_v1_backup"))

    def _add_missing_columns(self) -> None:
        """create_all never alters an existing table, so a database made by an earlier WebAtlas
        version is missing columns added since (e.g. the Phase 3 analysis columns). Add them."""
        insp = inspect(self.engine)
        if "scans" not in insp.get_table_names():
            return
        have = {c["name"] for c in insp.get_columns("scans")}
        for col in Scan.__table__.columns:
            if col.name in have:
                continue
            default = col.default.arg if col.default is not None else None
            coltype = col.type.compile(self.engine.dialect)
            ddl = f'ALTER TABLE scans ADD COLUMN "{col.name}" {coltype}'
            if default is not None:
                literal = f"'{default}'" if isinstance(default, str) else default
                ddl += f" DEFAULT {literal}"
            with self.engine.begin() as conn:
                conn.execute(text(ddl))

    def _import_legacy(self, row: dict) -> None:
        settings = json.loads(row.get("settings") or "{}")
        summary = json.loads(row.get("summary") or "{}")
        result = json.loads(row["result"]) if row.get("result") else {}
        status = row["status"] if row["status"] != "running" else "interrupted"
        with self.engine.begin() as conn:
            conn.execute(insert(Scan), {
                "id": row["id"], "target": row["target"], "seeds": json.dumps(settings.pop("seeds", [])),
                "settings": json.dumps(settings), "status": status, "error": row.get("error") or "",
                "stopped_early": summary.get("stopped_early", ""), "started_at": row["started_at"],
                "finished_at": row.get("finished_at"), "elapsed": summary.get("elapsed", 0.0),
                "pages": len(result.get("pages", [])), "documents": len(result.get("documents", [])),
                "failures": len(result.get("failures", [])),
                "robots_skipped": len(result.get("skipped_by_robots", [])),
                "requests": summary.get("requests", 0), "sitemap_urls": summary.get("sitemap_urls", 0),
                "sitemap_done": True, "doc_types": json.dumps(summary.get("doc_types", {})),
            })
            if result.get("pages"):
                conn.execute(insert(PageRow), [{**{k: v for k, v in p.items() if k in PageRow.__table__.columns},
                                                "scan_id": row["id"]} for p in result["pages"]])
            if result.get("documents"):
                conn.execute(insert(DocumentRow), [
                    {**{k: v for k, v in d.items() if k in DocumentRow.__table__.columns}, "scan_id": row["id"]}
                    for d in result["documents"]])
            issues = [{"scan_id": row["id"], "url": f["url"], "reason": f["reason"], "kind": "failure"}
                      for f in result.get("failures", [])]
            issues += [{"scan_id": row["id"], "url": u, "reason": "Blocked by robots.txt", "kind": "robots"}
                       for u in result.get("skipped_by_robots", [])]
            if issues:
                conn.execute(insert(IssueRow), issues)

    # ---------------------------------------------------------------- scans

    def create_scan(self, scan_id: str, target: str, seeds: list[str], settings: dict, started_at: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(Scan), {"id": scan_id, "target": target, "seeds": json.dumps(seeds),
                                        "settings": json.dumps(settings), "status": "running",
                                        "started_at": started_at})

    def set_status(self, scan_id: str, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(Scan).where(Scan.id == scan_id).values(**values))

    def get_scan(self, scan_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(Scan).where(Scan.id == scan_id)).mappings().first()
        return _scan_dict(row) if row else None

    def list_scans(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(Scan).order_by(Scan.started_at.desc()).limit(limit)).mappings().all()
        return [_scan_dict(r) for r in rows]

    def delete_scan(self, scan_id: str) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(delete(Scan).where(Scan.id == scan_id)).rowcount > 0

    # ---------------------------------------------------------------- rows

    def rows_after(self, model, scan_id: str, after_id: int, limit: int = 2000) -> list[dict]:
        """Rows with id > after_id, oldest first. The browser keeps the last id it has seen."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(model).where(model.scan_id == scan_id, model.id > after_id)
                                .order_by(model.id).limit(limit)).mappings().all()
        return [dict(r) for r in rows]

    def iter_rows(self, model, scan_id: str, batch: int = 5000) -> Iterator[dict]:
        """Every row of a scan, read in batches so exports of huge scans use little memory."""
        last = 0
        while True:
            rows = self.rows_after(model, scan_id, last, batch)
            if not rows:
                return
            yield from rows
            last = rows[-1]["id"]

    def add_findings(self, rows: list[dict]) -> None:
        if rows:
            with self.engine.begin() as conn:
                conn.execute(insert(FindingRow), rows)

    def severity_counts(self, scan_id: str) -> dict[str, int]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(FindingRow.severity, func.sum(FindingRow.count))
                                .where(FindingRow.scan_id == scan_id).group_by(FindingRow.severity)).all()
        return {sev: int(n) for sev, n in rows}

    def documents_for_analysis(self, scan_id: str):
        """(id, final_url, doc_type) for every document of a scan, oldest first."""
        with self.engine.connect() as conn:
            yield from conn.execute(select(DocumentRow.id, DocumentRow.final_url, DocumentRow.doc_type)
                                    .where(DocumentRow.scan_id == scan_id).order_by(DocumentRow.id))

    def clear_findings(self, scan_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(FindingRow).where(FindingRow.scan_id == scan_id))

    def count(self, model, scan_id: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(model).where(model.scan_id == scan_id)).scalar()

    def doc_type_counts(self, scan_id: str) -> dict[str, int]:
        """{doc_type: count} for a scan, read back from the stored documents. Used to seed a
        resumed scan's running totals so its type counts aren't reset to the new run only."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(DocumentRow.doc_type, func.count()).where(DocumentRow.scan_id == scan_id)
                                .group_by(DocumentRow.doc_type)).all()
        return {t: n for t, n in rows}

    # ---------------------------------------------------------------- resume

    def resume_state(self, scan_id: str) -> ResumeState:
        scan = self.get_scan(scan_id)
        if scan is None:
            raise KeyError(scan_id)
        with self.engine.connect() as conn:
            pending, seen = [], set()
            for r in conn.execute(select(FrontierRow).where(FrontierRow.scan_id == scan_id)).mappings():
                seen.add(f"{r['kind']}:{r['url']}")
                if not r["done"]:
                    pending.append(QueueItem(r["priority"], 0, r["kind"], r["url"], r["depth"],
                                             r["source_page"], r["link_text"], r["from_sitemap"]))
            # Pages reached through a redirect are recorded under their final URL; mark those seen too.
            for (url,) in conn.execute(select(PageRow.url).where(PageRow.scan_id == scan_id)):
                seen.add(f"page:{url}")
            for (url,) in conn.execute(select(IssueRow.url).where(IssueRow.scan_id == scan_id,
                                                                 IssueRow.kind == "trap")):
                seen.add(f"trap:{url}")
            doc_keys = set()
            for url, final, sha in conn.execute(select(DocumentRow.url, DocumentRow.final_url, DocumentRow.sha256)
                                                .where(DocumentRow.scan_id == scan_id)):
                doc_keys |= {f"url:{url}", f"url:{final}"} | ({f"sha:{sha}"} if sha else set())
        pending.sort()
        return ResumeState(pending=pending, seen=seen, doc_keys=doc_keys,
                           pages_done=scan["pages"], documents_done=scan["documents"],
                           doc_checks=scan["doc_checks"], failures=scan["failures"],
                           robots_skipped=scan["robots_skipped"], requests=scan["requests"],
                           sitemap_urls=scan["sitemap_urls"], sitemap_done=scan["sitemap_done"])


def _scan_dict(row) -> dict:
    d = dict(row)
    d["seeds"] = json.loads(d["seeds"] or "[]")
    d["settings"] = json.loads(d["settings"] or "{}")
    d["doc_types"] = json.loads(d["doc_types"] or "{}")
    d["severity_counts"] = json.loads(d.get("severity_counts") or "{}")
    return d


class DbSink:
    """Writes a crawl's results to the database in batches while it runs.

    Called from the crawl's event loop; rows are buffered and written every `interval`
    seconds or `batch_size` rows (one transaction per flush). Call flush() at the end."""

    def __init__(self, db: Database, scan_id: str, interval: float = 0.5, batch_size: int = 500):
        self.db, self.scan_id = db, scan_id
        self.interval, self.batch_size = interval, batch_size
        self._lock = threading.Lock()
        self._pages: list[dict] = []
        self._docs: list[dict] = []
        self._issues: list[dict] = []
        self._queued: list[dict] = []
        self._done: list[tuple[str, str]] = []
        self._doc_types: dict[str, int] = {}
        self._sitemap_urls: int | None = None
        self._last_flush = time.monotonic()
        self.counters: dict = {}

    def _maybe_flush(self) -> None:
        pending = len(self._pages) + len(self._docs) + len(self._issues) + len(self._queued) + len(self._done)
        if pending >= self.batch_size or time.monotonic() - self._last_flush >= self.interval:
            self.flush()

    def page(self, page: Page) -> None:
        with self._lock:
            self._pages.append({**_row(PageRow, page), "scan_id": self.scan_id})
        self._maybe_flush()

    def document(self, document: Document) -> None:
        with self._lock:
            self._docs.append({**_row(DocumentRow, document), "scan_id": self.scan_id})
            self._doc_types[document.doc_type] = self._doc_types.get(document.doc_type, 0) + 1
        self._maybe_flush()

    def failure(self, failure: Failure, kind: str = "failure") -> None:
        with self._lock:
            self._issues.append({"scan_id": self.scan_id, "url": failure.url, "reason": failure.reason, "kind": kind})
        self._maybe_flush()

    def robots_skip(self, url: str, reason: str) -> None:
        with self._lock:
            self._issues.append({"scan_id": self.scan_id, "url": url, "reason": reason, "kind": "robots"})
        self._maybe_flush()

    def enqueued(self, item: QueueItem) -> None:
        with self._lock:
            self._queued.append({"scan_id": self.scan_id, "kind": item.kind, "url": item.url, "depth": item.depth,
                                 "priority": item.priority, "source_page": item.source_page,
                                 "link_text": item.link_text, "from_sitemap": item.from_sitemap})
        self._maybe_flush()

    def processed(self, item: QueueItem) -> None:
        with self._lock:
            self._done.append((item.kind, item.url))
        self._maybe_flush()

    def sitemap_done(self, urls: int) -> None:
        with self._lock:
            self._sitemap_urls = urls
        self.flush()

    def flush(self, **scan_values) -> None:
        """Write everything buffered, plus counters and any extra scan columns, in one transaction."""
        with self._lock:
            pages, docs, issues = self._pages, self._docs, self._issues
            queued, done = self._queued, self._done
            self._pages, self._docs, self._issues, self._queued, self._done = [], [], [], [], []
            doc_types = dict(self._doc_types)
            sitemap_urls, self._sitemap_urls = self._sitemap_urls, None
            counters = dict(self.counters)
            self._last_flush = time.monotonic()
        with self.db.engine.begin() as conn:
            if queued:
                conn.execute(insert(FrontierRow).prefix_with("OR IGNORE"), queued)
            if pages:
                conn.execute(insert(PageRow), pages)
            if docs:
                conn.execute(insert(DocumentRow), docs)
            if issues:
                conn.execute(insert(IssueRow), issues)
            for kind in ("page", "doc"):
                urls = [u for k, u in done if k == kind]
                for i in range(0, len(urls), 500):
                    conn.execute(update(FrontierRow).where(FrontierRow.scan_id == self.scan_id,
                                                           FrontierRow.kind == kind,
                                                           FrontierRow.url.in_(urls[i:i + 500])).values(done=True))
            values = {**counters, **scan_values}
            if doc_types:
                values["doc_types"] = json.dumps(doc_types)
            if sitemap_urls is not None:
                values.update(sitemap_done=True, sitemap_urls=sitemap_urls)
            if values:
                conn.execute(update(Scan).where(Scan.id == self.scan_id).values(**values))
