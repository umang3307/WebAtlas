"""Phase 4 web endpoints: /api/ai, /api/scans/<id>/index, /index/stop and /ask.

The scan is seeded directly in the database (analysis is covered elsewhere); a fake model is
injected through create_app, so there's no network and no real model.
"""

from __future__ import annotations

import time

import pytest

from webatlas.db import DocumentRow
from webatlas.web import create_app

from .fake_llm import make_client

DOCS = [
    ("http://x/budget.pdf", "Budget 2025", "pdf",
     "The budget allocates two million dollars to road repair and public transit this year."),
    ("http://x/policy.docx", "Privacy Policy", "docx",
     "This privacy policy explains how we collect personal data. We retain it for five years."),
]


def _seed(db, scan_id="s1"):
    db.create_scan(scan_id, "http://x", ["http://x"], {}, "2026-01-01T00:00:00Z")
    db.set_status(scan_id, status="done", analysis_status="done", analyzed_docs=len(DOCS))
    rows = []
    for i, (url, title, dtype, text) in enumerate(DOCS, start=1):
        with db.engine.begin() as conn:
            conn.execute(DocumentRow.__table__.insert(),
                         {"id": i, "scan_id": scan_id, "url": url, "final_url": url,
                          "title": title, "doc_type": dtype})
        rows.append({"scan_id": scan_id, "document_id": i, "url": url, "title": title,
                     "doc_type": dtype, "text": text})
    db.store_content(rows)
    return scan_id


@pytest.fixture
def app(tmp_path):
    return create_app(db_path=tmp_path / "scans.db", allow_private=True, llm_client=make_client())


@pytest.fixture
def client(app):
    return app.test_client()


def _wait_index(client, scan_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ai = client.get(f"/api/scans/{scan_id}").get_json()["ai"]
        if ai["status"] in ("done", "error"):
            return ai
        time.sleep(0.05)
    raise AssertionError("indexing did not finish")


def test_ai_info_endpoint(client):
    info = client.get("/api/ai").get_json()
    assert info["available"] is True
    assert info["model"] == "fake-model"
    assert info["needs_key"] is False


def test_index_then_ask_flow(app, client):
    sid = _seed(app.config["MANAGER"].db)
    assert client.post(f"/api/scans/{sid}/index", json={}).status_code == 200
    ai = _wait_index(client, sid)
    assert ai["status"] == "done"
    assert ai["chunks"] >= 2
    assert ai["embed_kind"] == "model"

    res = client.post(f"/api/scans/{sid}/ask", json={"question": "how much for road repair?"})
    body = res.get_json()
    assert res.status_code == 200
    assert body["mode"] == "generated"
    assert body["used_model"] is True
    assert body["citations"]


def test_snapshot_exposes_ai_block(app, client):
    sid = _seed(app.config["MANAGER"].db)
    snap = client.get(f"/api/scans/{sid}").get_json()
    assert snap["indexable"] is True
    assert snap["ai"]["status"] == "none"


def test_index_without_analysis_is_rejected(client):
    # a scan with no stored content
    app_db = client.application.config["MANAGER"].db
    app_db.create_scan("bare", "http://x", [], {}, "2026-01-01T00:00:00Z")
    app_db.set_status("bare", status="done")
    res = client.post("/api/scans/bare/index", json={})
    assert res.status_code == 409


def test_ask_empty_question_is_400(app, client):
    sid = _seed(app.config["MANAGER"].db)
    assert client.post(f"/api/scans/{sid}/ask", json={"question": "  "}).status_code == 400


def test_ask_unknown_scan_is_404(client):
    assert client.post("/api/scans/nope/ask", json={"question": "hi"}).status_code == 404


def test_stop_index_when_none_running_is_409(app, client):
    sid = _seed(app.config["MANAGER"].db)
    assert client.post(f"/api/scans/{sid}/index/stop", json={}).status_code == 409
