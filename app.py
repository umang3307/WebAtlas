"""
WebAtlas web server (v2, Phase 1: self-hosted crawler, no third-party scraping API).

Run with:  python app.py   then open http://localhost:5000

Flow:
  1. /api/discover   reads the homepage and lists its sections
  2. /api/scan       crawls the selected sections with webatlas.crawler
  3. results are written to the SQLite graph (Domain -> Page -> Document)
  4. the frontend polls /api/scan/<id>/status and renders the graph when done

Phase 2 replaces Flask + in-memory jobs with FastAPI and resumable background jobs.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

from db import database
from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler, discover_sections
from webatlas.urls import normalize_url

app = Flask(__name__, static_folder="frontend")

JOBS: dict[str, dict] = {}            # scan jobs: job_id -> status
DISCOVERY_JOBS: dict[str, dict] = {}  # discovery jobs: job_id -> status

MAX_PAGES_DEFAULT = 30
MAX_PAGES_LIMIT = 500
DEFAULT_DEPTH = 1   # follow links one hop beyond the selected sections
_YEAR = re.compile(r"(19[89]\d|20\d\d)")
# Development only: WEBATLAS_ALLOW_PRIVATE=1 lets the app crawl localhost / private IPs.
ALLOW_PRIVATE = os.environ.get("WEBATLAS_ALLOW_PRIVATE") == "1"


SCAN_TIME_LIMIT = 600   # seconds; a scan returns what it found so far after this


def _config(**overrides) -> CrawlConfig:
    return CrawlConfig(allow_private_networks=ALLOW_PRIVATE, max_duration=SCAN_TIME_LIMIT, **overrides)


def _set(job_id, status, message, **extra):
    JOBS[job_id] = {"status": status, "message": message, **extra}


def _guess_year(*texts: str) -> str:
    """Notices and circulars usually carry their year in the title or file name."""
    for text in texts:
        match = _YEAR.search(text or "")
        if match:
            return match.group(1)
    return ""


def _run_discovery_job(job_id, root_url):
    try:
        DISCOVERY_JOBS[job_id] = {"status": "discovering", "message": "Reading the homepage..."}
        sections, homepage_docs = asyncio.run(discover_sections(root_url, _config()))
        DISCOVERY_JOBS[job_id] = {
            "status": "done", "sections": sections, "homepage_doc_count": len(homepage_docs),
        }
    except Exception as e:
        DISCOVERY_JOBS[job_id] = {"status": "error", "message": str(e)}


@app.route("/api/discover", methods=["POST"])
def start_discovery():
    root_url = normalize_url((request.json or {}).get("url", ""))
    if not root_url:
        return jsonify({"error": "a valid http(s) url is required"}), 400
    job_id = str(uuid.uuid4())
    DISCOVERY_JOBS[job_id] = {"status": "queued", "message": "Queued..."}
    threading.Thread(target=_run_discovery_job, args=(job_id, root_url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/discover/<job_id>/status")
def discover_status(job_id):
    return jsonify(DISCOVERY_JOBS.get(job_id, {"status": "unknown"}))


def _run_scan_job(job_id, root_url, seed_urls, max_pages):
    try:
        database.init_db()
        domain = urlparse(root_url).netloc
        scan_id = database.start_scan(domain)
        _set(job_id, "discovering", "Reading robots.txt and sitemap...", scan_id=scan_id)

        def on_event(event):
            if event["type"] in ("fetching", "page"):
                done = event.get("pages_done", 0)
                _set(job_id, "scraping", f"Crawling page {done + 1}/{event['max_pages']}: {event['url']}",
                     scan_id=scan_id)

        config = _config(max_depth=DEFAULT_DEPTH, max_pages=max_pages)
        seeds = seed_urls or [root_url]
        result = asyncio.run(Crawler(config, on_event=on_event).crawl(seeds))

        if result.failures:
            database.log_scrape(
                scan_id, "partial", notes=f"{len(result.failures)} URL(s) failed: "
                + "; ".join(f"{f.url} ({f.reason})" for f in result.failures[:10]))
        if result.skipped_by_robots:
            skipped = len(result.skipped_by_robots)
            database.log_scrape(scan_id, "skipped", notes=f"{skipped} URL(s) skipped because robots.txt disallows them")

        if not result.pages and not result.documents:
            database.log_scrape(scan_id, "failed", notes="no pages could be fetched")
            _set(job_id, "error", "Couldn't fetch any pages. Check the URL is reachable and public.",
                 scan_id=scan_id)
            return

        _set(job_id, "building", f"Building the knowledge graph from {len(result.pages)} pages...",
             scan_id=scan_id)
        domain_node_id = database.add_node(scan_id, "Domain", domain, url=root_url)
        page_nodes: dict[str, int] = {}

        for doc in result.documents:
            page_url = doc.source_page or root_url
            if page_url not in page_nodes:
                page_nodes[page_url] = database.add_node(scan_id, "Page", page_url, url=page_url)
                database.add_edge(scan_id, domain_node_id, page_nodes[page_url], "LINKS_TO")
            year = _guess_year(doc.title, doc.final_url, doc.last_modified)
            doc_node_id = database.add_node(
                scan_id, "Document", doc.title, url=doc.final_url, file_type=doc.doc_type,
                discovered_via=f"discovered ({year or 'undated'})",
                raw_data={**doc.__dict__},
            )
            database.add_edge(scan_id, page_nodes[page_url], doc_node_id, "LINKS_TO")
            database.add_edge(scan_id, doc_node_id, domain_node_id, "HOSTED_ON")

        database.log_scrape(
            scan_id, "success",
            notes=f"crawled {len(result.pages)} pages, found {len(result.documents)} documents "
                  f"across {len(page_nodes)} pages ({result.sitemap_urls} URLs from sitemaps)")
        database.finish_scan(scan_id)
        message = f"Crawled {len(result.pages)} pages, found {len(result.documents)} documents."
        if result.stopped_early:
            message += f" Stopped early: {result.stopped_early}."
        _set(job_id, "done", message, scan_id=scan_id)
    except Exception as e:
        _set(job_id, "error", str(e))


@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.route("/api/scan", methods=["POST"])
def start_scan():
    body = request.json or {}
    target_url = normalize_url(body.get("url", ""))
    if not target_url:
        return jsonify({"error": "a valid http(s) url is required"}), 400
    seeds = [u for u in (normalize_url(s) for s in (body.get("seeds") or [])) if u] or None
    try:
        max_pages = min(int(body.get("max_pages") or MAX_PAGES_DEFAULT), MAX_PAGES_LIMIT)
    except (TypeError, ValueError):
        max_pages = MAX_PAGES_DEFAULT
    job_id = str(uuid.uuid4())
    _set(job_id, "queued", "Queued...")
    threading.Thread(target=_run_scan_job, args=(job_id, target_url, seeds, max_pages), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/scan/<job_id>/status")
def scan_status(job_id):
    return jsonify(JOBS.get(job_id, {"status": "unknown"}))


@app.route("/api/scan/<job_id>/graph")
def scan_graph(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "not ready"}), 400
    return jsonify(database.export_graph(job["scan_id"]))


if __name__ == "__main__":
    database.init_db()
    # Bound to localhost on purpose: this is a local tool, not a public service (yet).
    app.run(host="127.0.0.1", port=5000, debug=False)
