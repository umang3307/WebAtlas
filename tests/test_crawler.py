import pytest

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler, discover_sections

from .fake_site import FakeSite

ROOT = "http://college.test/"


def config(**overrides) -> CrawlConfig:
    # The fake site's hosts don't resolve in DNS, so tests turn the SSRF DNS check off
    # here and test it separately in test_safety.py.
    base = dict(per_host_delay=0, retries=1, allow_private_networks=True, max_depth=2, max_pages=50)
    base.update(overrides)
    return CrawlConfig(**base)


async def run(site: FakeSite, seeds=(ROOT,), **overrides):
    events = []
    result = await Crawler(config(**overrides), transport=site.transport(), on_event=events.append).crawl(list(seeds))
    return result, events


def doc_urls(result) -> set[str]:
    return {d.final_url for d in result.documents}


async def test_finds_documents_by_every_signal():
    site = FakeSite()
    result, _ = await run(site)
    docs = {d.final_url: d for d in result.documents}

    assert docs["http://college.test/notices/circular-2024.pdf"].detected_by == "content-type"
    # Served as application/octet-stream: only the %PDF- magic bytes reveal it.
    assert docs["http://college.test/download.php?id=5"].doc_type == "pdf"
    assert docs["http://college.test/download.php?id=5"].detected_by == "magic-bytes"
    # HEAD returns 405, so the crawler peeks at the first bytes instead.
    assert docs["http://college.test/notices/old-rules.doc"].doc_type == "office-legacy"
    assert docs["http://college.test/tenders/t-2023.xlsx"].doc_type == "xlsx"
    # Found after a 503 + retry.
    assert "http://college.test/results/merit-list.pdf" in docs


async def test_link_text_becomes_title_and_source_page_is_kept():
    result, _ = await run(FakeSite())
    doc = next(d for d in result.documents if d.final_url.endswith("circular-2024.pdf"))
    assert doc.title == "Fee circular 2024"
    assert doc.source_page == "http://college.test/notices"
    assert doc.last_modified.startswith("Tue, 02 Jan 2024")


async def test_duplicate_links_with_tracking_params_are_deduped():
    result, _ = await run(FakeSite())
    circulars = [d for d in result.documents if "circular-2024" in d.final_url]
    assert len(circulars) == 1


async def test_fake_pdf_link_that_serves_html_is_reported_not_recorded():
    result, _ = await run(FakeSite())
    assert "http://college.test/notices/fake.pdf" not in doc_urls(result)
    reasons = {f.url: f.reason for f in result.failures}
    assert "served as text/html" in reasons["http://college.test/notices/fake.pdf"]
    assert reasons["http://college.test/notices/missing.pdf"] == "HTTP 404"


async def test_respects_robots_txt():
    site = FakeSite()
    result, _ = await run(site)
    assert not site.fetched("http://college.test/private/staff")
    assert "http://college.test/private/staff" in result.skipped_by_robots


async def test_robots_can_be_turned_off():
    site = FakeSite()
    await run(site, respect_robots=False)
    assert site.fetched("http://college.test/private/staff")


async def test_reads_gzipped_sitemap_index():
    site = FakeSite()
    result, _ = await run(site)
    # /about is only listed in the sitemap, never linked from a page.
    assert any(p.url == "http://college.test/about" for p in result.pages)
    assert "http://college.test/files/annual-report.pdf" in doc_urls(result)
    assert "http://college.test/files/prospectus-2026.pdf" in doc_urls(result)
    assert result.sitemap_urls == 2   # elsewhere.test was out of scope
    assert not site.fetched("http://elsewhere.test/not-in-scope")


async def test_sitemap_can_be_turned_off():
    result, _ = await run(FakeSite(), use_sitemaps=False)
    assert "http://college.test/files/prospectus-2026.pdf" not in doc_urls(result)


async def test_never_crawls_other_sites_but_keeps_their_documents():
    site = FakeSite()
    result, _ = await run(site)
    assert not any(method == "GET" and url.startswith("https://other.test") for method, url in site.requests)
    assert "https://cdn.test/files/brochure.pdf" in doc_urls(result)


async def test_offsite_documents_can_be_excluded():
    result, _ = await run(FakeSite(), include_offsite_documents=False)
    assert "https://cdn.test/files/brochure.pdf" not in doc_urls(result)


async def test_depth_limit():
    result, _ = await run(FakeSite(), max_depth=1)
    # /tenders/page2 is two hops away, so its spreadsheet is never found.
    assert "http://college.test/tenders/t-2023.xlsx" not in doc_urls(result)
    assert all(p.depth <= 1 for p in result.pages)


async def test_page_budget():
    result, _ = await run(FakeSite(), max_pages=2)
    assert len(result.pages) <= 2


async def test_redirect_is_followed_and_not_crawled_twice():
    site = FakeSite()
    result, _ = await run(site)
    page_urls = [p.url for p in result.pages]
    assert page_urls.count("http://college.test/notices") == 1


async def test_emits_progress_events():
    _, events = await run(FakeSite())
    kinds = {e["type"] for e in events}
    assert {"page", "document", "done"} <= kinds


async def test_rejects_non_http_seed():
    with pytest.raises(ValueError):
        await Crawler(config()).crawl(["ftp://college.test/"])


async def test_discover_sections_lists_homepage_links():
    site = FakeSite()
    sections, docs = await discover_sections(ROOT, config(), transport=site.transport())
    assert "http://college.test/notices" in sections
    assert "http://college.test/tenders/" in sections
    assert not any("other.test" in s for s in sections)
    assert "http://college.test/private/staff" not in sections   # blocked by robots.txt
    assert docs == ["https://cdn.test/files/brochure.pdf"]


async def test_doc_check_budget_lists_remaining_documents_unverified():
    site = FakeSite()
    result, _ = await run(site, max_doc_checks=2)
    unverified = [d for d in result.documents if d.detected_by == "extension-unverified"]
    assert unverified, "documents past the budget should still be listed"
    # Unverified documents were never requested.
    assert not any(site.fetched(d.url) for d in unverified)
    # 2 checked doc links + /download.php?id=5 (found as a page, so it costs no doc check).
    assert sum(1 for d in result.documents if d.detected_by != "extension-unverified") <= 3


async def test_time_limit_returns_partial_results():
    import asyncio as _asyncio

    import httpx

    def slow_handler(request):
        return FakeSite().handler(request)

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await _asyncio.sleep(0.2)
            return slow_handler(request)

    cfg = config(max_duration=0.5, concurrency=1)
    result = await Crawler(cfg, transport=SlowTransport()).crawl([ROOT])
    assert result.stopped_early.startswith("time limit")
    assert len(result.pages) < 5
    assert result.finished_at


async def test_counts_requests():
    result, _ = await run(FakeSite())
    assert result.requests > len(result.pages)


async def test_all_limits_can_be_lifted():
    result, _ = await run(FakeSite(), max_depth=None, max_pages=None, max_doc_checks=None,
                          max_documents=None, max_sitemap_urls=None)
    assert "http://college.test/tenders/t-2023.xlsx" in doc_urls(result)
    assert not result.stopped_early


async def test_crawler_traps_are_skipped():
    site = FakeSite()
    # A page whose relative link resolves deeper every time: /loop/a/ -> /loop/a/a/ -> ...
    site.routes["http://college.test/"] = (200, {"content-type": "text/html"}, b'<a href="/loop/a/">loop</a>')
    for depth in range(1, 12):
        url = "http://college.test/loop/" + "a/" * depth
        site.routes[url] = (200, {"content-type": "text/html"}, b'<a href="a/">deeper</a>')
    result, _ = await run(site, max_depth=None, max_pages=None, use_sitemaps=False)
    deepest = max(p.url.count("/a") for p in result.pages)
    assert deepest <= 3
    assert any("crawler trap" in f.reason for f in result.failures)
