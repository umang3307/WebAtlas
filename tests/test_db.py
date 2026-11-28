"""Phase 2: database storage, resume, migration and streaming export."""

import asyncio
import json
import sqlite3

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.db import Database, DbSink, DocumentRow, FindingRow, IssueRow, PageRow
from webatlas.web import create_app

from .fake_site import FakeSite

ROOT = "http://college.test/"


def cfg(**over):
    base = dict(per_host_delay=0, retries=1, allow_private_networks=True, max_depth=2, max_pages=50)
    base.update(over)
    return CrawlConfig(**base)


async def crawl_to_db(db, site, scan_id="s1", resume=None, **over):
    db.create_scan(scan_id, ROOT, [ROOT], {}, "2026-11-28T00:00:00+00:00") if resume is None else None
    sink = DbSink(db, scan_id, interval=0, batch_size=1)   # flush eagerly so tests see every row
    crawler = Crawler(cfg(**over), transport=site.transport(), sink=sink)
    result = await crawler.crawl([ROOT], resume=resume)
    sink.flush(status="done", pages=result.pages_count, documents=result.documents_count,
               failures=result.failures_count, robots_skipped=result.robots_skipped_count,
               requests=result.requests, doc_checks=crawler._doc_checks, sitemap_urls=result.sitemap_urls)
    return crawler, result


# ---------------------------------------------------------------- storage


def test_results_are_written_to_the_database_during_the_crawl(tmp_path):
    db = Database(tmp_path / "s.db")
    crawler, result = asyncio.run(crawl_to_db(db, FakeSite()))
    assert db.count(DocumentRow, "s1") == result.documents_count > 5
    assert db.count(PageRow, "s1") == result.pages_count > 0
    assert db.count(IssueRow, "s1") >= 1
    scan = db.get_scan("s1")
    assert scan["documents"] == result.documents_count
    assert scan["doc_types"]["pdf"] >= 1


def test_crawl_result_keeps_counts_not_lists_with_a_db_sink(tmp_path):
    db = Database(tmp_path / "s.db")
    _crawler, result = asyncio.run(crawl_to_db(db, FakeSite()))
    # Memory stays flat: the lists are empty, only counters grow.
    assert result.documents == [] and result.pages == []
    assert result.documents_count > 0 and result.pages_count > 0


def test_rows_after_is_an_incremental_cursor(tmp_path):
    db = Database(tmp_path / "s.db")
    asyncio.run(crawl_to_db(db, FakeSite()))
    first = db.rows_after(DocumentRow, "s1", 0, limit=3)
    assert len(first) == 3
    nxt = db.rows_after(DocumentRow, "s1", first[-1]["id"], limit=3)
    assert all(r["id"] > first[-1]["id"] for r in nxt)
    assert {r["id"] for r in first} & {r["id"] for r in nxt} == set()


def test_deleting_a_scan_removes_its_rows(tmp_path):
    db = Database(tmp_path / "s.db")
    asyncio.run(crawl_to_db(db, FakeSite()))
    assert db.delete_scan("s1") is True
    assert db.count(DocumentRow, "s1") == 0 and db.count(PageRow, "s1") == 0
    assert db.get_scan("s1") is None


# ---------------------------------------------------------------- resume


def test_interrupted_scan_resumes_without_refetching(tmp_path):
    db = Database(tmp_path / "s.db")
    site = FakeSite()

    # Stop the first run after its first page, using the crawler's own event callback
    # (per-instance, so this test never touches global state and stays order-independent).
    db.create_scan("r1", ROOT, [ROOT], {}, "2026-11-28T00:00:00+00:00")
    sink = DbSink(db, "r1", interval=0, batch_size=1)
    crawler = Crawler(cfg(concurrency=1), transport=site.transport(), sink=sink)

    def stop_after_first_page(event):
        if event["type"] == "page" and crawler.result.pages_count >= 1:
            crawler.request_stop()

    crawler.on_event = stop_after_first_page
    result1 = asyncio.run(crawler.crawl([ROOT]))
    sink.flush(status="interrupted", pages=result1.pages_count, documents=result1.documents_count,
               failures=result1.failures_count, robots_skipped=result1.robots_skipped_count,
               requests=result1.requests, doc_checks=crawler._doc_checks, sitemap_urls=result1.sitemap_urls)
    assert 0 < db.count(PageRow, "r1") < 10, "run 1 crawled some pages but not all"
    pages_done_in_run1 = {r["url"] for r in db.rows_after(PageRow, "r1", 0)}

    # Resume: a fresh crawler continues from the saved frontier.
    site.requests.clear()
    resume = db.resume_state("r1")
    crawler2 = Crawler(cfg(), transport=site.transport(), sink=DbSink(db, "r1", interval=0, batch_size=1))
    result2 = asyncio.run(crawler2.crawl([ROOT], resume=resume))
    run2_gets = {u for m, u in site.requests if m == "GET"}

    # A full crawl from scratch, for comparison.
    full_db = Database(tmp_path / "full.db")
    _c, full = asyncio.run(crawl_to_db(full_db, FakeSite(), scan_id="full"))

    assert db.count(PageRow, "r1") == full.pages_count
    assert db.count(DocumentRow, "r1") == full.documents_count
    assert result2.pages_count == full.pages_count
    # Pages already crawled in run 1 are never fetched again in run 2 (robots.txt may be, that's fine).
    assert pages_done_in_run1 & run2_gets == set()


def test_resume_continues_counters(tmp_path):
    db = Database(tmp_path / "s.db")
    asyncio.run(crawl_to_db(db, FakeSite(), scan_id="c1"))
    db.set_status("c1", status="stopped")
    before = db.get_scan("c1")["requests"]
    resume = db.resume_state("c1")
    # Nothing left to do, but the resumed run must not reset the request count to zero.
    _c, result = asyncio.run(crawl_to_db(db, FakeSite(), scan_id="c1", resume=resume))
    assert result.requests >= before


# ---------------------------------------------------------------- migration


def test_migrates_v1_json_blob_schema(tmp_path):
    db_path = tmp_path / "old.db"
    # Re-create the old v2.0 single-table schema and one finished scan.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE scans (id TEXT PRIMARY KEY, target TEXT, status TEXT, settings TEXT,
                            summary TEXT, result TEXT, error TEXT, started_at TEXT, finished_at TEXT);
    """)
    result = {"pages": [{"url": "http://x.edu/a", "depth": 0, "status": 200, "title": "A",
                         "links_found": 2, "documents_found": 1, "rendered": False, "found_on": ""}],
              "documents": [{"url": "http://x.edu/a.pdf", "final_url": "http://x.edu/a.pdf", "title": "Doc",
                             "source_page": "http://x.edu/a", "doc_type": "pdf", "content_type": "application/pdf",
                             "size_bytes": 10, "last_modified": "", "sha256": None, "detected_by": "content-type"}],
              "failures": [{"url": "http://x.edu/bad", "reason": "HTTP 404"}],
              "skipped_by_robots": ["http://x.edu/private"]}
    conn.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?)",
                 ("old1", "http://x.edu/", "done", json.dumps({"max_pages": 50, "seeds": ["http://x.edu/"]}),
                  json.dumps({"documents": 1, "pages": 1, "elapsed": 3.2, "doc_types": {"pdf": 1}}),
                  json.dumps(result), "", "2026-11-01T00:00:00+00:00", "2026-11-01T00:01:00+00:00"))
    conn.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?)",
                 ("old2", "http://y.edu/", "running", "{}", "{}", None, "", "2026-11-01T00:00:00+00:00", None))
    conn.commit()
    conn.close()

    db = Database(db_path)   # triggers migration
    scan = db.get_scan("old1")
    assert scan is not None and scan["documents"] == 1 and scan["pages"] == 1
    assert scan["doc_types"] == {"pdf": 1} and scan["seeds"] == ["http://x.edu/"]
    assert db.count(DocumentRow, "old1") == 1 and db.count(PageRow, "old1") == 1
    assert db.count(IssueRow, "old1") == 2   # one failure + one robots skip
    # A scan left "running" in the old file is marked interrupted, not lost.
    assert db.get_scan("old2")["status"] == "interrupted"
    # The old table is gone and the new schema is in place.
    names = {r[0] for r in sqlite3.connect(db_path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scans_v1_backup" not in names and "frontier" in names and "meta" in names


def test_running_scans_become_interrupted_on_restart(tmp_path):
    db = Database(tmp_path / "s.db")
    db.create_scan("x", ROOT, [ROOT], {}, "2026-11-28T00:00:00+00:00")
    assert db.get_scan("x")["status"] == "running"
    reopened = Database(tmp_path / "s.db")   # like restarting the server
    assert reopened.get_scan("x")["status"] == "interrupted"


# ---------------------------------------------------------------- streaming export


def _client(tmp_path, site, **over):
    return create_app(db_path=tmp_path / "s.db", allow_private=True, transport=site.transport()).test_client()


def _wait(client, scan_id, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end:
        data = client.get(f"/api/scans/{scan_id}").get_json()
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_streaming_csv_and_json_export(tmp_path):
    client = _client(tmp_path, FakeSite())
    scan_id = client.post("/api/scans", json={"url": ROOT, "settings": {"per_host_delay": 0}}).get_json()["id"]
    done = _wait(client, scan_id)

    csv_res = client.get(f"/api/scans/{scan_id}/export?format=csv")
    lines = csv_res.data.decode().strip().splitlines()
    assert lines[0].startswith("doc_type,title,final_url")
    assert len(lines) - 1 == done["summary"]["documents"]   # one row per document, all of them

    doc = json.loads(client.get(f"/api/scans/{scan_id}/export?format=json").data)
    assert doc["id"] == scan_id
    assert len(doc["documents"]) == done["summary"]["documents"]
    assert len(doc["pages"]) == done["summary"]["pages"]
    assert "issues" in doc


def test_export_404_for_unknown_scan(tmp_path):
    client = create_app(db_path=tmp_path / "s.db", allow_private=True).test_client()
    assert client.get("/api/scans/nope/export?format=csv").status_code == 404


def test_doc_types_survive_a_resume(tmp_path):
    from webatlas.web.jobs import ScanManager

    db = Database(tmp_path / "s.db")
    mgr = ScanManager(db, allow_private=True, transport=FakeSite().transport())

    job = mgr.start(ROOT, [ROOT], {"per_host_delay": 0, "max_pages": 50})
    job.thread.join(10)
    full_types = db.get_scan(job.id)["doc_types"]
    assert full_types.get("pdf", 0) > 0

    # Pretend it was only partway: mark it stopped, then resume. Types must not shrink.
    db.set_status(job.id, status="stopped")
    resumed = mgr.resume(job.id)
    resumed.thread.join(10)
    after = db.get_scan(resumed.id)["doc_types"]
    assert after.get("pdf", 0) >= full_types.get("pdf", 0)
    assert after.get("xlsx", 0) >= full_types.get("xlsx", 0)


def test_three_scans_write_to_one_database_concurrently(tmp_path):
    from webatlas.web.jobs import ScanManager

    db = Database(tmp_path / "shared.db")
    mgr = ScanManager(db, allow_private=True, transport=FakeSite().transport())
    jobs = [mgr.start(ROOT, [ROOT], {"per_host_delay": 0, "max_pages": 50}) for _ in range(3)]
    for j in jobs:
        j.thread.join(20)
    for j in jobs:
        scan = db.get_scan(j.id)
        assert scan["status"] == "done", f"{j.id}: {scan['status']} {scan['error']}"
        assert scan["documents"] > 5
        assert db.count(DocumentRow, j.id) == scan["documents"]


def test_phase2_database_gains_analysis_columns(tmp_path):
    """A database made before Phase 3 has no analysis columns; opening it must add them."""
    import sqlite3
    db_path = tmp_path / "p2.db"
    # Minimal Phase 2 'scans' table (no analysis_status/findings/severity_counts columns).
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE scans (id TEXT PRIMARY KEY, target TEXT, seeds TEXT, settings TEXT,
        status TEXT, error TEXT, stopped_early TEXT, started_at TEXT, finished_at TEXT, elapsed REAL,
        pages INTEGER, documents INTEGER, failures INTEGER, robots_skipped INTEGER, requests INTEGER,
        doc_checks INTEGER, sitemap_urls INTEGER, sitemap_done INTEGER, doc_types TEXT)""")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO scans (id, target, status, started_at, documents, pages) VALUES "
                 "('old', 'http://x/', 'done', '2026-11-01T00:00:00+00:00', 3, 2)")
    conn.commit()
    conn.close()

    db = Database(db_path)   # should ALTER TABLE to add the new columns
    scan = db.get_scan("old")
    assert scan["analysis_status"] == "none"       # new column, default applied
    assert scan["findings"] == 0
    assert scan["severity_counts"] == {}
    assert scan["documents"] == 3                  # old data preserved


def test_findings_table_gains_secret_column(tmp_path):
    """A database whose 'findings' table predates the reveal/hide feature must gain the 'secret'
    column when opened, so existing analyses keep working."""
    import sqlite3
    db_path = tmp_path / "old_findings.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE findings (id INTEGER PRIMARY KEY, scan_id TEXT, document_id INTEGER,
        url TEXT, doc_type TEXT, rule TEXT, severity TEXT, title TEXT, location TEXT, detail TEXT,
        count INTEGER)""")   # note: no 'secret' column
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    db = Database(db_path)   # should ALTER TABLE findings ADD COLUMN secret
    db.create_scan("s", "http://x/", [], {}, "2026-11-01T00:00:00+00:00")
    db.add_findings([{"scan_id": "s", "document_id": 1, "url": "http://x/f.pdf", "doc_type": "pdf",
                      "rule": "aws-access-key", "severity": "critical", "title": "AWS key",
                      "location": "text", "detail": "AKI***EF", "secret": "AKIAFULLVALUE123456",
                      "count": 1}])
    row = db.rows_after(FindingRow, "s", 0)[0]
    assert row["detail"] == "AKI***EF"
    assert row["secret"] == "AKIAFULLVALUE123456"   # full value round-trips for reveal
