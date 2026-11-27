"""WebAtlas web interface: a local scan console.

Start it with `webatlas ui` (or `python app.py`) and open http://127.0.0.1:5000.

API (JSON):
  GET    /api/info                       version, whether browser rendering is installed
  POST   /api/discover   {url}           the homepage's sections, to pick seeds from
  POST   /api/scans      {url, seeds, settings}  start a scan
  GET    /api/scans                      scan history
  GET    /api/scans/<id>?events=&documents=&pages=&issues=   progress since those cursors
  POST   /api/scans/<id>/stop            finish early, keep results
  DELETE /api/scans/<id>                 remove from history
  GET    /api/scans/<id>/export?format=json|csv
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import json
import os
from pathlib import Path

import httpx
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from webatlas import __version__
from webatlas.config import CrawlConfig
from webatlas.crawler import discover_sections
from webatlas.fetcher import Fetcher, FetchError
from webatlas.safety import Resolver, UnsafeTargetError, default_resolver
from webatlas.storage import ScanStore
from webatlas.urls import normalize_url
from webatlas.web.jobs import ScanManager, TooManyScans, clean_settings

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
CSV_FIELDS = ["doc_type", "title", "final_url", "source_page", "detected_by", "content_type",
              "size_bytes", "last_modified", "sha256", "url"]


def create_app(
    db_path: str | Path | None = None,
    allow_private: bool | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = default_resolver,
    extra_hosts: set[str] | None = None,
) -> Flask:
    if allow_private is None:
        allow_private = os.environ.get("WEBATLAS_ALLOW_PRIVATE") == "1"
    store = ScanStore(db_path or os.environ.get("WEBATLAS_DB", "webatlas.db"))
    manager = ScanManager(store, allow_private=allow_private, transport=transport, resolver=resolver)
    allowed_hosts = LOCAL_HOSTS | (extra_hosts or set())

    app = Flask(__name__, static_folder=None)
    app.config["MANAGER"] = manager

    @app.before_request
    def protect_local_api():
        # DNS rebinding: a hostile page could point its own domain at 127.0.0.1 and call this API.
        # Only answer requests addressed to a local host name.
        host = (request.host or "").rsplit(":", 1)[0] if not request.host.startswith("[") \
            else request.host.split("]")[0] + "]"
        if host not in allowed_hosts:
            abort(403, description="WebAtlas only answers on localhost")
        # CSRF: browsers can't send application/json across origins without a CORS preflight,
        # which this server never approves.
        if request.method in ("POST", "DELETE") and request.path.startswith("/api/") and not request.is_json:
            abort(415, description="send JSON")

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(415)
    def json_error(err):
        return jsonify({"error": getattr(err, "description", str(err))}), err.code

    @app.after_request
    def security_headers(resp: Response):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return resp

    # ---------------------------------------------------------------- pages

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC, filename)

    # ---------------------------------------------------------------- api

    @app.get("/api/info")
    def info():
        return jsonify({
            "version": __version__,
            "render_available": importlib.util.find_spec("playwright") is not None,
            "allow_private": allow_private,
            "defaults": {k: getattr(CrawlConfig, k) for k in (
                "max_depth", "max_pages", "max_doc_checks", "concurrency", "per_host_delay",
                "use_sitemaps", "include_subdomains", "include_offsite_documents", "render_js",
                "download_documents")},
        })

    async def _pick_scheme(https_url: str) -> str:
        """For an address typed without http(s)://, use https if the site answers on it,
        otherwise fall back to http - the same thing browsers do. The SSRF guard still applies."""
        config = CrawlConfig(allow_private_networks=allow_private, retries=0, timeout=8, per_host_delay=0)
        async with Fetcher(config, transport=transport, resolver=resolver) as fetcher:
            try:
                await fetcher.peek(https_url, n_bytes=512)
                return https_url
            except FetchError:
                return "http://" + https_url.split("://", 1)[1]

    def _url_from_body() -> str:
        url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
        typed_scheme = "://" in url
        if url and not typed_scheme:
            url = "https://" + url   # people paste "example.edu"
        normalized = normalize_url(url)
        if not normalized:
            abort(400, description="Enter a website address, for example www.example.edu")
        if not typed_scheme:
            try:
                normalized = asyncio.run(_pick_scheme(normalized))
            except UnsafeTargetError as exc:
                abort(400, description=f"Can't scan this address: {exc}")
        return normalized

    @app.post("/api/discover")
    def discover():
        url = _url_from_body()
        config = CrawlConfig(allow_private_networks=allow_private, retries=1, timeout=15)
        try:
            sections, docs = asyncio.run(discover_sections(url, config, transport=transport, resolver=resolver))
        except UnsafeTargetError as exc:
            return jsonify({"error": f"Can't scan this address: {exc}"}), 400
        except Exception as exc:
            return jsonify({"error": f"Couldn't read the homepage: {exc}"}), 502
        return jsonify({"url": url, "sections": sections, "documents": docs})

    @app.post("/api/scans")
    def start_scan():
        body = request.get_json(silent=True) or {}
        url = _url_from_body()
        seeds = [s for s in (normalize_url(x) for x in (body.get("seeds") or [])[:200]) if s]
        settings = clean_settings(body.get("settings") or {})
        try:
            job = manager.start(url, seeds, settings)
        except TooManyScans as exc:
            return jsonify({"error": str(exc)}), 429
        return jsonify({"id": job.id}), 201

    @app.get("/api/scans")
    def list_scans():
        return jsonify({"scans": store.list()})

    @app.get("/api/scans/<scan_id>")
    def scan(scan_id):
        cursors = {k: request.args.get(k, 0, type=int) for k in ("events", "documents", "pages", "issues")}
        snap = manager.snapshot(scan_id, cursors)
        if snap is None:
            abort(404, description="No scan with that id")
        return jsonify(snap)

    @app.post("/api/scans/<scan_id>/stop")
    def stop_scan(scan_id):
        if not manager.stop(scan_id):
            return jsonify({"error": "That scan isn't running"}), 409
        return jsonify({"ok": True})

    @app.delete("/api/scans/<scan_id>")
    def delete_scan(scan_id):
        job = manager.jobs.get(scan_id)
        if job and job.status == "running":
            return jsonify({"error": "Stop the scan before deleting it"}), 409
        manager.jobs.pop(scan_id, None)
        if not store.delete(scan_id):
            abort(404, description="No scan with that id")
        return jsonify({"ok": True})

    @app.get("/api/scans/<scan_id>/export")
    def export(scan_id):
        snap = manager.snapshot(scan_id, {})
        if snap is None:
            abort(404, description="No scan with that id")
        host = (snap["target"].split("/")[2] if "://" in snap["target"] else "scan").replace(":", "_")
        if request.args.get("format") == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for doc in snap["documents"]:
                # Neutralise spreadsheet formulas (CSV injection) in text that came from websites.
                writer.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@\t\r" else v)
                                 for k, v in doc.items()})
            return Response(buf.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": f'attachment; filename="webatlas-{host}-{scan_id}.csv"'})
        payload = {k: snap[k] for k in ("id", "target", "status", "started_at", "settings", "summary",
                                         "documents", "pages", "issues")}
        return Response(json.dumps(payload, indent=2), mimetype="application/json", headers={
            "Content-Disposition": f'attachment; filename="webatlas-{host}-{scan_id}.json"'})

    return app
