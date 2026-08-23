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
from scraper.brightdata_client import run_collector, CollectorNotFoundError
from scraper.collector_manager import get_or_create_collector, heal_collector, invalidate_collector
from scraper.generic_extractor import infer_file_type
from scraper.crawler import crawl_site, discover_sections, MAX_PAGES_DEFAULT

app = Flask(__name__, static_folder="frontend")
JOBS = {}  # job_id -> {"status": ..., "message": ..., "scan_id": ...}


def _set(job_id, status, message, **extra):
    JOBS[job_id] = {"status": status, "message": message, **extra}


DISCOVERY_JOBS = {}

GENERIC_HEAL_PROMPT = (
    "This page returned no usable internal links or documents on the last run. "
    "The extraction schema may not match this site's structure (for example, it "
    "may have mistaken this page for an e-commerce product page instead of a "
    "normal site). Return two arrays instead: 'internal_links' should list every "
    "full URL to another page on this same website found in the navigation, "
    "header, or footer. 'documents' should list every downloadable file (PDF, "
    "DOC, DOCX, XLS, XLSX, PPT, CSV) linked anywhere on the page, each with "
    "title, document_url, and date if shown."
)

def _run_discovery_job(job_id, root_url):
    try:
        DISCOVERY_JOBS[job_id] = {"status": "creating_scraper", "message": "Setting up scraper..."}
        collector_id = get_or_create_collector(
            root_url,
            status_cb=lambda msg: DISCOVERY_JOBS.__setitem__(job_id, {"status": "creating_scraper", "message": msg})
        )
        DISCOVERY_JOBS[job_id] = {"status": "discovering", "message": "Scanning homepage for sections..."}
        try:
            sections, homepage_doc_count = discover_sections(root_url, collector_id, run_collector)
        except CollectorNotFoundError:
            DISCOVERY_JOBS[job_id] = {
                "status": "creating_scraper",
                "message": "Cached scraper no longer exists on Bright Data — rebuilding it..."
            }
            invalidate_collector(root_url)
            collector_id = get_or_create_collector(
                root_url,
                status_cb=lambda msg: DISCOVERY_JOBS.__setitem__(job_id, {"status": "creating_scraper", "message": msg})
            )
            DISCOVERY_JOBS[job_id] = {"status": "discovering", "message": "Scanning homepage for sections..."}
            sections, homepage_doc_count = discover_sections(root_url, collector_id, run_collector)

        # The collector responded, but a fresh AI-generated scraper sometimes
        # misreads an ordinary homepage as an e-commerce product page and
        # returns no real navigation links -- self-heal instead of reporting
        # a false "this site has 1 section" result.
        if not sections and homepage_doc_count == 0:
            DISCOVERY_JOBS[job_id] = {
                "status": "healing",
                "message": "No sections found — the scraper may have misread this page. Healing it..."
            }
            healed = heal_collector(
                collector_id, GENERIC_HEAL_PROMPT,
                status_cb=lambda msg: DISCOVERY_JOBS.__setitem__(job_id, {"status": "healing", "message": msg})
            )
            if healed:
                DISCOVERY_JOBS[job_id] = {"status": "discovering", "message": "Heal applied — rescanning homepage..."}
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

        try:
            docs, pages_crawled, visited_urls, failed_urls = crawl_site(
                seeds, domain, collector_id, run_collector,
                max_pages=max_pages or MAX_PAGES_DEFAULT, max_depth=0, status_cb=_crawl_status
            )
        except CollectorNotFoundError:
            _set(job_id, "creating_scraper",
                 "Cached scraper no longer exists on Bright Data — rebuilding it...", scan_id=scan_id)
            invalidate_collector(root_url)
            collector_id = get_or_create_collector(
                root_url,
                status_cb=lambda msg: _set(job_id, "creating_scraper", msg, scan_id=scan_id)
            )
            docs, pages_crawled, visited_urls, failed_urls = crawl_site(
                seeds, domain, collector_id, run_collector,
                max_pages=max_pages or MAX_PAGES_DEFAULT, max_depth=0, status_cb=_crawl_status
            )

        if not docs and pages_crawled == 0:
            database.log_scrape(scan_id, "failed", collector_id, "",
                                 "crawl did not run — 0 pages reached, likely a network/API/rate-limit issue. "
                                 f"Selected {len(seeds)} section(s), all failed: "
                                 + "; ".join(f"{f['url']} ({f['error']})" for f in failed_urls[:10]))
            _set(job_id, "error",
                 "Couldn't reach the collector at all (0 pages crawled). This looks like a "
                 "connectivity or rate-limit issue, not a bad collector — check your Bright Data "
                 "dashboard and network before retrying.", scan_id=scan_id)
            return

        if not docs:
            _set(job_id, "healing", f"No documents found after {pages_crawled} pages — attempting to heal the collector...", scan_id=scan_id)
            healed = heal_collector(
                collector_id, GENERIC_HEAL_PROMPT,
                status_cb=lambda msg: _set(job_id, "healing", msg, scan_id=scan_id)
            )

            if healed:
                _set(job_id, "scraping", "Heal applied — retrying the crawl...", scan_id=scan_id)
                docs, pages_crawled, visited_urls, failed_urls = crawl_site(
                    seeds, domain, collector_id, run_collector,
                    max_pages=max_pages or MAX_PAGES_DEFAULT, max_depth=0, status_cb=_crawl_status
                )

            if not docs:
                database.log_scrape(scan_id, "failed", collector_id, "",
                                     f"crawled {pages_crawled} pages, no documents found (heal attempted: {healed}). "
                                     + (f"Failed pages: " + "; ".join(f"{f['url']} ({f['error']})" for f in failed_urls[:10]) if failed_urls else ""))
                _set(job_id, "error", f"Crawled {pages_crawled} pages but found no documents, even after a heal attempt.", scan_id=scan_id)
                return
            else:
                database.log_scrape(scan_id, "healed", collector_id, "",
                                     f"auto-heal fixed a broken extraction — found {len(docs)} documents after retry")

        if failed_urls:
            database.log_scrape(scan_id, "partial", collector_id, "",
                                 f"{len(failed_urls)} of {len(seeds)} selected page(s) could not be crawled: "
                                 + "; ".join(f"{f['url']} ({f['error']})" for f in failed_urls[:10]))

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

        done_msg = f"Crawled {pages_crawled} pages, found {doc_count} documents."
        if failed_urls:
            done_msg += (f" {len(failed_urls)} of {len(seeds)} selected page(s) failed to crawl "
                         f"(see self-heal log below) — try 'Scan selected' again to retry just those.")
        _set(job_id, "done", done_msg, scan_id=scan_id)

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