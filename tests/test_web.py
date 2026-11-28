import time

import pytest

from webatlas.web import create_app
from webatlas.web.jobs import MAX_RUNNING, clean_settings

from .fake_site import FakeSite

ROOT = "http://college.test/"
FAST = {"per_host_delay": 0, "max_depth": 2, "max_pages": 50}


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def client(tmp_path, site):
    app = create_app(db_path=tmp_path / "scans.db", allow_private=True, transport=site.transport())
    return app.test_client()


def wait_done(client, scan_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/scans/{scan_id}").get_json()
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def start(client, **body):
    body.setdefault("url", ROOT)
    body.setdefault("settings", FAST)
    return client.post("/api/scans", json=body)


def test_index_and_static_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and b"WebAtlas" in page.data
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../web/__init__.py").status_code == 404


def test_full_scan_flow(client):
    res = start(client)
    assert res.status_code == 201
    scan_id = res.get_json()["id"]
    data = wait_done(client, scan_id)
    assert data["status"] == "done"
    assert data["summary"]["documents"] == len(data["documents"]) > 5
    assert any(d["final_url"].endswith("download.php?id=5") for d in data["documents"])
    assert any(i["kind"] == "robots" for i in data["issues"])
    assert data["events"] and data["events_total"] >= len(data["events"])
    assert any(p["found_on"] for p in data["pages"])


def test_cursors_return_only_new_items(client):
    scan_id = start(client).get_json()["id"]
    full = wait_done(client, scan_id)
    n_docs, n_pages = len(full["documents"]), len(full["pages"])
    tail = client.get(f"/api/scans/{scan_id}?documents={n_docs}&pages={n_pages - 1}").get_json()
    assert tail["documents"] == []
    assert len(tail["pages"]) == 1


def test_history_and_reload_from_database(tmp_path, site):
    db = tmp_path / "scans.db"
    client = create_app(db_path=db, allow_private=True, transport=site.transport()).test_client()
    scan_id = start(client).get_json()["id"]
    done = wait_done(client, scan_id)
    history = client.get("/api/scans").get_json()["scans"]
    assert history[0]["id"] == scan_id and history[0]["summary"]["documents"] == len(done["documents"])

    # A fresh server (e.g. after a restart) serves the same scan from SQLite.
    fresh = create_app(db_path=db, allow_private=True, transport=site.transport()).test_client()
    again = fresh.get(f"/api/scans/{scan_id}").get_json()
    assert again["status"] == "done"
    assert len(again["documents"]) == len(done["documents"])
    assert len(again["issues"]) == len(done["issues"])


def test_export_csv_and_json(client):
    scan_id = start(client).get_json()["id"]
    wait_done(client, scan_id)
    csv_res = client.get(f"/api/scans/{scan_id}/export?format=csv")
    assert csv_res.mimetype == "text/csv"
    assert "attachment" in csv_res.headers["Content-Disposition"]
    assert csv_res.data.decode().splitlines()[0].startswith("doc_type,title,final_url")
    json_res = client.get(f"/api/scans/{scan_id}/export?format=json")
    assert json_res.get_json()["id"] == scan_id


def test_csv_neutralises_spreadsheet_formulas(tmp_path):
    site = FakeSite()
    site.routes["http://college.test/notices"] = (200, {"content-type": "text/html"},
        b'<a href="/notices/circular-2024.pdf">=HYPERLINK("http://evil.test","click")</a>')
    client = create_app(db_path=tmp_path / "s.db", allow_private=True, transport=site.transport()).test_client()
    scan_id = start(client, settings={**FAST, "use_sitemaps": False}).get_json()["id"]
    wait_done(client, scan_id)
    body = client.get(f"/api/scans/{scan_id}/export?format=csv").data.decode()
    assert "'=HYPERLINK" in body


def test_stop_returns_partial_results(tmp_path):
    import asyncio

    import httpx

    site = FakeSite()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.15)
            return site.handler(request)

    client = create_app(db_path=tmp_path / "s.db", allow_private=True, transport=Slow()).test_client()
    scan_id = start(client, settings={**FAST, "concurrency": 1}).get_json()["id"]
    time.sleep(0.4)
    assert client.post(f"/api/scans/{scan_id}/stop", json={}).status_code == 200
    data = wait_done(client, scan_id)
    assert data["status"] == "stopped"
    assert data["summary"]["stopped_early"] == "stopped by user"
    # Stopping a finished scan is refused.
    assert client.post(f"/api/scans/{scan_id}/stop", json={}).status_code == 409


def test_delete(client):
    scan_id = start(client).get_json()["id"]
    wait_done(client, scan_id)
    assert client.delete(f"/api/scans/{scan_id}", json={}).status_code == 200
    assert client.get(f"/api/scans/{scan_id}").status_code == 404
    assert client.delete(f"/api/scans/{scan_id}", json={}).status_code == 404


def test_limits_running_scans(tmp_path):
    import asyncio

    import httpx

    site = FakeSite()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.3)
            return site.handler(request)

    app = create_app(db_path=tmp_path / "s.db", allow_private=True, transport=Slow())
    client = app.test_client()
    ids = [start(client).get_json()["id"] for _ in range(MAX_RUNNING)]
    assert start(client).status_code == 429
    for scan_id in ids:
        client.post(f"/api/scans/{scan_id}/stop", json={})


def test_rejects_bad_urls_and_private_targets(tmp_path, site):
    client = create_app(db_path=tmp_path / "s.db", allow_private=False).test_client()
    assert start(client, url="javascript:alert(1)").status_code == 400
    assert start(client, url="").status_code == 400
    res = client.post("/api/discover", json={"url": "http://127.0.0.1:6379/"})
    assert res.status_code == 400 and "Can't scan" in res.get_json()["error"]


def test_bare_domain_falls_back_to_http(client):
    # The fake site only answers on http://, like a server without TLS.
    data = client.post("/api/discover", json={"url": "college.test"}).get_json()
    assert data["url"] == "http://college.test/"


def test_bare_domain_prefers_https(tmp_path):
    import httpx

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<a href='/a'>a</a>")

    client = create_app(db_path=tmp_path / "s.db", allow_private=True,
                        transport=httpx.MockTransport(handler)).test_client()
    assert client.post("/api/discover", json={"url": "secure.test"}).get_json()["url"] == "https://secure.test/"


def test_bare_private_address_is_refused(tmp_path):
    client = create_app(db_path=tmp_path / "s.db", allow_private=False).test_client()
    res = client.post("/api/scans", json={"url": "127.0.0.1:6379"})
    assert res.status_code == 400 and "Can't scan" in res.get_json()["error"]


def test_discover(client):
    data = client.post("/api/discover", json={"url": ROOT}).get_json()
    assert "http://college.test/notices" in data["sections"]
    assert "http://college.test/private/staff" not in data["sections"]


def test_rejects_non_json_posts_csrf(client):
    res = client.post("/api/scans", data="url=http://college.test/", content_type="application/x-www-form-urlencoded")
    assert res.status_code == 415


def test_rejects_foreign_host_header_dns_rebinding(client):
    res = client.get("/api/scans", headers={"Host": "evil.example:5000"})
    assert res.status_code == 403


def test_settings_are_clamped():
    s = clean_settings({"max_pages": 10**12, "max_depth": -3, "concurrency": "abc", "render_js": 1, "x": 5})
    assert s["max_pages"] == 10_000_000 and s["max_depth"] == 0
    assert "concurrency" not in s and "x" not in s
    assert s["render_js"] is True
    assert s["max_duration"] == 600   # a missing time limit gets the default


def test_limits_can_be_lifted_with_null_but_politeness_cannot():
    s = clean_settings({"max_pages": None, "max_depth": None, "max_duration": None, "max_doc_checks": None,
                        "concurrency": None, "per_host_delay": None})
    assert s == {"max_pages": None, "max_depth": None, "max_duration": None, "max_doc_checks": None}


def test_unlimited_scan_through_the_console(client):
    res = start(client, settings={"per_host_delay": 0, "max_pages": None, "max_depth": None,
                                  "max_duration": None, "max_doc_checks": None})
    data = wait_done(client, res.get_json()["id"])
    assert data["status"] == "done" and data["max_pages"] is None
    # With no depth limit the two-hop spreadsheet is found and nothing is left unconfirmed.
    assert any(d["final_url"].endswith("t-2023.xlsx") for d in data["documents"])
    assert not any(d["detected_by"] == "extension-unverified" for d in data["documents"])


# ---------------------------------------------------------------- quality-review regressions


def test_concurrent_starts_cannot_exceed_the_running_limit(tmp_path):
    import asyncio
    import threading

    import httpx

    site = FakeSite()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.5)
            return site.handler(request)

    app = create_app(db_path=tmp_path / "s.db", allow_private=True, transport=Slow())
    codes, barrier = [], threading.Barrier(20)

    def fire():
        barrier.wait()
        codes.append(app.test_client().post("/api/scans", json={"url": ROOT, "settings": FAST}).status_code)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(201) == MAX_RUNNING and codes.count(429) == 20 - MAX_RUNNING
    manager = app.config["MANAGER"]
    for scan_id in list(manager.jobs):
        manager.stop(scan_id)


def test_partial_results_are_saved_when_a_scan_crashes(tmp_path, site, monkeypatch):
    from webatlas.crawler import Crawler

    original = Crawler._until_done_or_stopped

    async def crash_after_work(self, task):
        await original(self, task)
        if self.result.pages_count:
            raise RuntimeError("simulated crash after finding pages")

    monkeypatch.setattr(Crawler, "_until_done_or_stopped", crash_after_work)
    # Make the final save slow so a scan that showed as finished before it was saved would be caught.
    from webatlas.db import DbSink
    real_flush = DbSink.flush

    def slow_flush(self, **kwargs):
        if kwargs.get("status"):     # the final flush carries the status
            time.sleep(0.3)
        return real_flush(self, **kwargs)

    monkeypatch.setattr(DbSink, "flush", slow_flush)
    client = create_app(db_path=tmp_path / "s.db", allow_private=True, transport=site.transport()).test_client()
    scan_id = start(client).get_json()["id"]
    data = wait_done(client, scan_id)
    assert data["status"] == "error" and "simulated crash" in data["error"]

    # Reload from the database, as after a restart: what was found before the crash is kept.
    saved = create_app(db_path=tmp_path / "s.db", allow_private=True).test_client() \
        .get(f"/api/scans/{scan_id}").get_json()
    assert saved["status"] == "error" and len(saved["pages"]) >= 1


def test_negative_cursors_are_clamped(client):
    scan_id = start(client).get_json()["id"]
    full = wait_done(client, scan_id)
    again = client.get(f"/api/scans/{scan_id}?documents=-5&pages=-5").get_json()
    assert len(again["documents"]) == len(full["documents"])


def test_errors_are_always_json(client):
    res = client.put("/api/scans")
    assert res.status_code == 405 and "error" in res.get_json()
