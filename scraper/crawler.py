"""
Crawls an entire site (or subdomain) starting from one URL, using ONE
collector for every page — the collector is asked for both internal links
(the crawl frontier) and documents (the payload) on each page.

Capped by MAX_PAGES and MAX_DEPTH so a demo run finishes in a reasonable
time instead of trying to crawl an entire university website exhaustively.
Raise these if you have time to spare.
"""
from collections import deque
from urllib.parse import urlparse

from scraper.generic_extractor import extract_documents, extract_links
from scraper.brightdata_client import CollectorNotFoundError

MAX_PAGES_DEFAULT = 25
MAX_DEPTH_DEFAULT = 2


def crawl_site(seed_urls, root_netloc, collector_id, run_collector_fn,
                max_pages=MAX_PAGES_DEFAULT, max_depth=MAX_DEPTH_DEFAULT, status_cb=None):
    """
    seed_urls: list of one or more starting URLs (e.g. user-selected sections
    like /notices, /tenders). All seeds start at depth 0; the crawl follows
    same-site links up to max_depth beyond each seed.

    Returns (all_docs, pages_crawled, pages_visited_urls, failed_urls).
    all_docs: list of {"document_url","title","date","page_url"}
    failed_urls: list of {"url", "error"} for pages the collector could not
    fetch -- surfaced to the caller instead of being silently dropped, so a
    partial crawl (e.g. some selected sections failing) is visible rather
    than looking like those sections were simply never selected.
    """
    visited = set()
    frontier = deque((url, 0) for url in seed_urls)
    all_docs = []
    pages_crawled = 0
    failed_urls = []

    while frontier and pages_crawled < max_pages:
        url, depth = frontier.popleft()
        norm = url.rstrip("/")
        if norm in visited:
            continue
        visited.add(norm)

        if status_cb:
            status_cb(pages_crawled, max_pages, url)

        try:
            records, _snapshot_id = run_collector_fn(collector_id, url)
        except CollectorNotFoundError:
            # The collector itself is dead -- every remaining page will fail
            # the same way, so stop here and let the caller rebuild it
            # instead of burning through the whole crawl one 404 at a time.
            raise
        except Exception as e:
            failed_urls.append({"url": url, "error": str(e)})
            if status_cb:
                status_cb(pages_crawled, max_pages, f"{url} — FAILED: {e}")
            continue

        pages_crawled += 1
        if not records:
            failed_urls.append({"url": url, "error": "collector returned no records"})
            continue

        docs = extract_documents(records, page_url_hint=url)
        all_docs.extend(docs)

        if depth < max_depth:
            links = extract_links(records, root_netloc)
            for link in links:
                link_norm = link.rstrip("/")
                if link_norm not in visited:
                    frontier.append((link, depth + 1))

    return all_docs, pages_crawled, visited, failed_urls


def discover_sections(root_url, collector_id, run_collector_fn):
    root_netloc = urlparse(root_url).netloc
    root_norm = root_url.rstrip("/")
    try:
        records, _snapshot_id = run_collector_fn(collector_id, root_url)
    except CollectorNotFoundError:
        raise  # preserve type so callers can trigger a rebuild
    except Exception as e:
        raise RuntimeError(f"discover_sections failed calling collector: {e}")
    if not records:
        return [], 0
    links = extract_links(records, root_netloc)
    links.discard(root_url)
    links = {l for l in links if l.rstrip("/") != root_norm}
    homepage_docs = extract_documents(records, page_url_hint=root_url)
    return sorted(links), len(homepage_docs)