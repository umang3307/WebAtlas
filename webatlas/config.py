"""Settings for one crawl. Every limit has a safe default."""

from __future__ import annotations

from dataclasses import dataclass

from webatlas import USER_AGENT


@dataclass
class CrawlConfig:
    # How far and how wide to go. Any of these can be None, meaning "no limit".
    # The defaults are deliberately modest so a casual scan of a huge site (say, Wikipedia)
    # can't run for days; lift them when you mean to.
    max_depth: int | None = 2            # link hops away from a seed page
    max_pages: int | None = 100          # HTML pages fetched (documents don't count)
    max_documents: int | None = None     # documents recorded
    max_doc_checks: int | None = 1000    # documents confirmed with a request; beyond this they're listed
                                         # from their link alone and marked "extension-unverified"
    max_duration: float | None = None    # seconds; when reached, stop and return what was found so far

    # Politeness.
    concurrency: int = 4          # pages fetched at the same time (across all hosts)
    per_host_delay: float = 0.5   # minimum seconds between requests to one host
    respect_robots: bool = True
    user_agent: str = USER_AGENT

    # Network limits.
    timeout: float = 20.0
    retries: int = 2              # extra attempts after timeouts, 429 and 5xx
    max_redirects: int = 5
    max_page_bytes: int = 5 * 1024 * 1024        # stop reading an HTML page after 5 MB
    max_document_bytes: int = 50 * 1024 * 1024   # skip downloading documents bigger than 50 MB

    # Discovery.
    use_sitemaps: bool = True
    max_sitemap_urls: int | None = 50_000   # URLs read from sitemaps (None = all)
    include_subdomains: bool = True        # treat admissions.example.edu as part of example.edu
    include_offsite_documents: bool = True  # keep a PDF on a CDN if an in-scope page links to it

    # Documents.
    download_documents: bool = False  # download each document to hash it (SHA-256) and dedupe

    # Passive security scanning: inspect each page's own response (headers + body) for weak
    # security headers, insecure cookies, version disclosure, etc. Non-intrusive — no extra
    # requests, so it's on by default.
    passive_scan: bool = True

    # JavaScript fallback (needs the optional "render" extra + `playwright install chromium`).
    render_js: bool = False
    render_min_links: int = 3   # re-render a page with a browser if it has fewer links than this

    # Safety. Only turn this on for local testing: it allows crawling localhost / private IPs.
    allow_private_networks: bool = False
