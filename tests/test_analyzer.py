"""Phase 3: the Analyzer end to end (crawl -> download -> findings) and the web analyze flow."""

import asyncio
import json
import time

import httpx
import pytest

from webatlas.analyzer import Analyzer
from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.db import Database, DbSink, FindingRow
from webatlas.web import create_app

from .doc_fixtures import make_docx, make_pdf, make_xlsx
from .secret_samples import AWS_KEY, SLACK_TOKEN

PDF = make_pdf(f"Reach me at admin@acme.internal. AWS key {AWS_KEY}. "
               r"Path C:\Users\jdoe\budget.xlsx", author="Jane Doe", producer="Acme PDF 2.1")
DOCX = make_docx("STRICTLY CONFIDENTIAL. password = Tr0ub4dor&3xKf99", author="Bob", username="bsmith",
                 company="Acme Corp")
XLSX = make_xlsx(["Payroll", f"token {SLACK_TOKEN}"])

ROOT = "http://docs.test/"
ROUTES = {
    "http://docs.test/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
    "http://docs.test/": (200, {"content-type": "text/html"},
        b'<a href="/a.pdf">Report</a> <a href="/b.docx">Memo</a> <a href="/c.xlsx">Sheet</a> '
        b'<a href="/clean.txt">Readme</a>'),
    "http://docs.test/a.pdf": (200, {"content-type": "application/pdf"}, PDF),
    "http://docs.test/b.docx": (200, {"content-type":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, DOCX),
    "http://docs.test/c.xlsx": (200, {"content-type":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}, XLSX),
    "http://docs.test/clean.txt": (200, {"content-type": "text/plain"}, b"Just a friendly readme, nothing secret."),
}


def handler(request):
    url = str(request.url)
    if url not in ROUTES:
        return httpx.Response(404)
    status, headers, body = ROUTES[url]
    if request.method == "HEAD":
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, headers=headers, content=body)


def transport():
    return httpx.MockTransport(handler)


def crawl(db, scan_id="s1"):
    db.create_scan(scan_id, ROOT, [ROOT], {}, "2026-11-28T00:00:00+00:00")
    sink = DbSink(db, scan_id, interval=0, batch_size=1)
    cfg = CrawlConfig(per_host_delay=0, allow_private_networks=True, max_depth=1, max_pages=10)
    result = asyncio.run(Crawler(cfg, transport=transport(), sink=sink).crawl([ROOT]))
    sink.flush(status="done", documents=result.documents_count, pages=result.pages_count)
    return result


def analyze(db, scan_id="s1", **kw):
    a = Analyzer(db, scan_id, allow_private=True, transport=transport(), **kw)
    asyncio.run(a.run())
    return a


def findings(db, scan_id="s1"):
    return db.rows_after(FindingRow, scan_id, 0, limit=1000)


# ---------------------------------------------------------------- analyzer


def test_analyzer_finds_leaks_across_formats(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    a = analyze(db)
    rules = {f["rule"] for f in findings(db)}
    assert "aws-access-key" in rules          # from the PDF
    assert "confidential-marker" in rules     # from the docx
    assert "possible-secret" in rules         # docx password
    assert "slack-token" in rules             # from the xlsx
    assert "metadata-username" in rules       # docx lastModifiedBy
    assert a.analyzed == 4                     # pdf, docx, xlsx and the clean .txt were all downloaded


def test_clean_document_yields_no_findings_but_is_counted(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    analyze(db)
    txt = [f for f in findings(db) if f["url"].endswith("clean.txt")]
    assert txt == []
    scan = db.get_scan("s1")
    assert scan["analysis_status"] == "done"
    assert scan["analyzed_docs"] == 4          # all four analysable docs were downloaded
    assert scan["findings"] > 0


def test_secrets_are_masked_in_the_database(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    analyze(db)
    for f in findings(db):
        assert AWS_KEY not in f["detail"]
        assert "Tr0ub4dor&3xKf99" not in f["detail"]


def test_severity_counts_recorded(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    analyze(db)
    sev = db.get_scan("s1")["severity_counts"]
    assert sev.get("high", 0) >= 1 and sev.get("medium", 0) >= 1


def test_reanalyze_replaces_old_findings(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    analyze(db)
    first = len(findings(db))
    analyze(db)   # run again
    assert len(findings(db)) == first    # not doubled


def test_analysis_respects_document_cap(tmp_path):
    db = Database(tmp_path / "s.db")
    crawl(db)
    a = analyze(db, max_documents=1)
    assert a.analyzed == 1


# ---------------------------------------------------------------- web flow


def _client(tmp_path):
    return create_app(db_path=tmp_path / "s.db", allow_private=True, transport=transport()).test_client()


def _run_scan(client):
    scan_id = client.post("/api/scans", json={"url": ROOT, "settings": {"per_host_delay": 0, "max_depth": 1}}) \
        .get_json()["id"]
    end = time.time() + 10
    while time.time() < end:
        if client.get(f"/api/scans/{scan_id}").get_json()["status"] != "running":
            return scan_id
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def _wait_analysis(client, scan_id, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        snap = client.get(f"/api/scans/{scan_id}").get_json()
        if snap["analysis"]["status"] in ("done", "stopped", "error") and not snap["analysis"]["running"]:
            return snap
        time.sleep(0.05)
    raise AssertionError("analysis did not finish")


def test_web_analyze_flow(tmp_path):
    client = _client(tmp_path)
    scan_id = _run_scan(client)
    snap = client.get(f"/api/scans/{scan_id}").get_json()
    assert snap["analyzable"] is True and snap["analysis"]["status"] == "none"

    assert client.post(f"/api/scans/{scan_id}/analyze", json={}).status_code == 200
    snap = _wait_analysis(client, scan_id)
    assert snap["analysis"]["status"] == "done"
    assert snap["analysis"]["findings"] > 0
    rules = {f["rule"] for f in snap["findings"]}
    assert "aws-access-key" in rules
    assert all(f.get("id") for f in snap["findings"])   # rows carry ids for the cursor


def test_web_analyze_requires_finished_scan(tmp_path):
    client = create_app(db_path=tmp_path / "s.db", allow_private=True).test_client()
    assert client.post("/api/scans/nope/analyze", json={}).status_code == 404


def test_findings_in_json_export(tmp_path):
    client = _client(tmp_path)
    scan_id = _run_scan(client)
    client.post(f"/api/scans/{scan_id}/analyze", json={})
    _wait_analysis(client, scan_id)
    doc = json.loads(client.get(f"/api/scans/{scan_id}/export?format=json").data)
    assert "findings" in doc and len(doc["findings"]) > 0
    assert any(f["rule"] == "aws-access-key" for f in doc["findings"])


def test_cannot_delete_scan_while_analysis_running(tmp_path, monkeypatch):
    # Make analysis slow so we can catch it mid-run.
    import webatlas.analyzer as az
    real = az.Analyzer._analyze_one

    async def slow(self, *a, **k):
        await asyncio.sleep(0.2)
        return await real(self, *a, **k)

    monkeypatch.setattr(az.Analyzer, "_analyze_one", slow)
    client = _client(tmp_path)
    scan_id = _run_scan(client)
    client.post(f"/api/scans/{scan_id}/analyze", json={})
    time.sleep(0.25)
    assert client.delete(f"/api/scans/{scan_id}", json={}).status_code == 409
    client.post(f"/api/scans/{scan_id}/analyze/stop", json={})
    _wait_analysis(client, scan_id)


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_export_still_works_without_analysis(tmp_path, fmt):
    client = _client(tmp_path)
    scan_id = _run_scan(client)
    assert client.get(f"/api/scans/{scan_id}/export?format={fmt}").status_code == 200
