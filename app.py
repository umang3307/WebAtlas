"""
WebAtlas web server.
Run with: python app.py
Open: http://localhost:5000

Flow per scan:
  1. get_or_create_collector() -> reuses cached collector for this domain,
     or runs `bdata scraper create` automatically if it's a new domain
  2. run_collector() -> triggers + polls Bright Data for results
  3. extract_documents() -> generic recursive parse, works on any JSON shape
  4. results written into the graph DB with full provenance
  5. frontend polls /api/scan/<id>/status and renders the graph on completion
"""
from flask import Flask, request, jsonify, send_from_directory
import threading
import uuid
from urllib.parse import urlparse

from db import database
from scraper.brightdata_client import run_collector
from scraper.collector_manager import get_or_create_collector
from scraper.generic_extractor import infer_file_type
from scraper.crawler import crawl_site, discover_sections, MAX_PAGES_DEFAULT

app = Flask(__name__, static_folder="frontend")
JOBS = {}  # job_id -> {"status": ..., "message": ..., "scan_id": ...}


def _set(job_id, status, message, **extra):
    JOBS[job_id] = {"status": status, "message": message, **extra}


DISCOVERY_JOBS = {}


def _run_discovery_job(job_id, root_url):
    try:
        DISCOVERY_JOBS[job_id] = {"status": "creating_scraper", "message": "Setting up scraper..."}
        collector_id = get_or_create_collector(
            root_url,
            status_cb=lambda msg: DISCOVERY_JOBS.__setitem__(job_id, {"status": "creating_scraper", "message": msg})
        )
        DISCOVERY_JOBS[job_id] = {"status": "discovering", "message": "Scanning homepage for sections..."}
        sections, homepage_doc_count = discover_sections(root_url, collector_id, run_collector)
        DISCOVERY_JOBS[job_id] = {
            "status": "done", "sections": sections,
            "homepage_doc_count": homepage_doc_count, "collector_id": collector_id
        }
    except Exception as e:
        DISCOVERY_JOBS[job_id] = {"status": "error", "message": str(e)}


@app.route("/api/discover", methods=["POST"])
def start_discovery():
    root_url = (request.json or {}).get("url", "").strip()
    if not root_url:
        return jsonify({"error": "url required"}), 400
    job_id = str(uuid.uuid4())
    DISCOVERY_JOBS[job_id] = {"status": "queued", "message": "Queued..."}
    threading.Thread(target=_run_discovery_job, args=(job_id, root_url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/discover/<job_id>/status")
def discover_status(job_id):
    return jsonify(DISCOVERY_JOBS.get(job_id, {"status": "unknown"}))


def _run_scan_job(job_id, root_url, seed_urls=None, max_pages=None):
    try:
        _set(job_id, "queued", "Starting...")
        database.init_db()
        domain = urlparse(root_url).netloc

        collector_id = get_or_create_collector(
            root_url,
            status_cb=lambda msg: _set(job_id, "creating_scraper", msg)
        )

        scan_id = database.start_scan(domain, collector_id=collector_id)
        domain_node_id = database.add_node(scan_id, "Domain", domain, url=root_url)

        seeds = seed_urls if seed_urls else [root_url]

        def _crawl_status(pages_done, mp, current_url):
            _set(job_id, "scraping",
                 f"Crawling page {pages_done + 1}/{mp}: {current_url}",
                 scan_id=scan_id)

        docs, pages_crawled, visited_urls = crawl_site(
            seeds, domain, collector_id, run_collector,
            max_pages=max_pages or MAX_PAGES_DEFAULT, max_depth=0, status_cb=_crawl_status
        )

        if not docs:
            database.log_scrape(scan_id, "failed", collector_id, "", f"crawled {pages_crawled} pages, no documents found")
            _set(job_id, "error", f"Crawled {pages_crawled} pages but found no documents.", scan_id=scan_id)
            return

        _set(job_id, "building", f"Building the knowledge graph from {pages_crawled} pages...", scan_id=scan_id)

        page_nodes = {}
        seen_doc_urls = set()
        doc_count = 0

        for item in docs:
            doc_url = item["document_url"]
            if not doc_url or doc_url in seen_doc_urls:
                continue
            seen_doc_urls.add(doc_url)

            page_url = item["page_url"] or root_url
            if page_url not in page_nodes:
                page_node_id = database.add_node(scan_id, "Page", page_url, url=page_url)
                database.add_edge(scan_id, domain_node_id, page_node_id, "LINKS_TO")
                page_nodes[page_url] = page_node_id
            else:
                page_node_id = page_nodes[page_url]

            doc_node_id = database.add_node(
                scan_id, "Document", item["title"], url=doc_url,
                file_type=infer_file_type(doc_url),
                discovered_via=f"discovered ({item['date'] or 'undated'})",
                raw_data=item
            )
            database.add_edge(scan_id, page_node_id, doc_node_id, "LINKS_TO")
            database.add_edge(scan_id, doc_node_id, domain_node_id, "HOSTED_ON")
            doc_count += 1

        database.log_scrape(scan_id, "success", collector_id, "",
                             f"crawled {pages_crawled} pages, found {doc_count} documents across {len(page_nodes)} document-hosting pages")
        database.finish_scan(scan_id)

        _set(job_id, "done",
             f"Crawled {pages_crawled} pages, found {doc_count} documents.", scan_id=scan_id)

    except Exception as e:
        _set(job_id, "error", str(e))


@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.route("/api/scan", methods=["POST"])
def start_scan():
    body = request.json or {}
    target_url = body.get("url", "").strip()
    seed_urls = body.get("seeds") or None
    max_pages = body.get("max_pages")
    if not target_url:
        return jsonify({"error": "url required"}), 400
    job_id = str(uuid.uuid4())
    _set(job_id, "queued", "Queued...")
    threading.Thread(target=_run_scan_job, args=(job_id, target_url, seed_urls, max_pages), daemon=True).start()
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
    app.run(debug=True, port=5000)
