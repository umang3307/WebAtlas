import gzip

import pytest

from webatlas.detect import classify_response, doc_type_from_magic, doc_type_from_url, is_html
from webatlas.extract import extract_page
from webatlas.sitemap import parse_sitemap
from webatlas.urls import in_scope, looks_like_trap, normalize_url, registered_domain

from .fake_site import OLE2, PDF, XLSX

# ---------------------------------------------------------------- urls


@pytest.mark.parametrize("raw,expected", [
    ("HTTP://Example.com:80/a/../b/?b=2&a=1#top", "http://example.com/b/?a=1&b=2"),
    ("https://example.com:443/x", "https://example.com/x"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("https://example.com/x?utm_source=mail&id=3", "https://example.com/x?id=3"),
    ("https://example.com", "https://example.com/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["mailto:a@b.c", "javascript:void(0)", "tel:123", "#top", "", "ftp://x.y/z",
                                 "http://host:abc/", "https://javascript:alert(1)", "http://[::1/"])
def test_normalize_rejects_non_crawlable(raw):
    assert normalize_url(raw, "https://example.com/") is None


def test_normalize_resolves_relative_links():
    assert normalize_url("../files/a.pdf", "https://example.com/notices/2024/") == \
        "https://example.com/notices/files/a.pdf"


def test_registered_domain_handles_multi_part_suffixes():
    assert registered_domain("https://admissions.cs.example.ac.in/x") == "example.ac.in"
    assert registered_domain("localhost") == "localhost"


def test_scope():
    root = "https://www.example.edu/"
    assert in_scope("https://example.edu/a", root)
    assert in_scope("https://library.example.edu/a", root)
    assert not in_scope("https://library.example.edu/a", root, include_subdomains=False)
    assert not in_scope("https://example.com/a", root)
    # Each github.io site is its own site.
    assert not in_scope("https://other.github.io/", "https://me.github.io/")

# ---------------------------------------------------------------- detection


@pytest.mark.parametrize("url,expected", [
    ("https://x.edu/files/Notice%20One.PDF", "pdf"),
    ("https://x.edu/get?file=report.docx", "docx"),
    ("https://x.edu/download.php?id=5", None),
    ("https://x.edu/page.html", None),
    ("https://x.edu/v1.2/", None),
])
def test_doc_type_from_url(url, expected):
    assert doc_type_from_url(url) == expected


def test_magic_bytes():
    assert doc_type_from_magic(PDF) == "pdf"
    assert doc_type_from_magic(OLE2) == "office-legacy"
    assert doc_type_from_magic(XLSX) == "xlsx"
    assert doc_type_from_magic(b"<html>") is None


def test_classify_prefers_headers_then_magic():
    assert classify_response("https://x/a", "application/pdf", "", b"") == ("pdf", "content-type")
    assert classify_response("https://x/a", "application/octet-stream",
                             'attachment; filename="fees.xlsx"', b"") == ("xlsx", "content-disposition")
    assert classify_response("https://x/a", "application/octet-stream", "", PDF) == ("pdf", "magic-bytes")
    assert classify_response("https://x/a.pdf", "text/html", "", b"<html>") is None


def test_is_html_sniffs_untyped_pages():
    assert is_html("", b"  <!DOCTYPE html><html>")
    assert not is_html("application/pdf", PDF)

# ---------------------------------------------------------------- extraction


def test_trap_heuristics():
    assert looks_like_trap("https://x.edu/a/b/a/b/a/b/a/b/")
    assert looks_like_trap("https://x.edu/?" + "s=1&" * 600)
    assert not looks_like_trap("https://x.edu/notices/2024/01/fee-circular.pdf")
    assert not looks_like_trap("https://x.edu/docs/docs/")   # one repeat is normal


def test_one_malformed_link_does_not_lose_the_others():
    _title, links = extract_page('<a href="http://x.edu:bad/">bad</a><a href="/ok.pdf">ok</a>', "https://x.edu/")
    assert [link.url for link in links] == ["https://x.edu/ok.pdf"]


def test_extract_page_links_and_base_href():
    html = """<html><head><title> Notices  Page </title><base href="https://x.edu/docs/">
      <link rel="stylesheet" href="/style.css"><link rel="alternate" href="/feed"></head>
      <body><a href="a.pdf">  Fee   notice </a><a href="a.pdf"></a><a href="mailto:x@y.z">m</a>
      <iframe src="/embed/viewer"></iframe>
      <meta http-equiv="refresh" content="5; url=/moved"></body></html>"""
    title, links = extract_page(html, "https://x.edu/notices")
    by_url = {link.url: link.text for link in links}
    assert title == "Notices Page"
    assert by_url["https://x.edu/docs/a.pdf"] == "Fee notice"
    assert "https://x.edu/feed" in by_url
    assert "https://x.edu/embed/viewer" in by_url
    assert "https://x.edu/moved" in by_url
    assert "https://x.edu/style.css" not in by_url

# ---------------------------------------------------------------- sitemaps


def test_parse_sitemap_urlset_and_index():
    urlset = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc> https://x/a </loc></url></urlset>'
    assert parse_sitemap(urlset) == (["https://x/a"], [])
    index = b'<sitemapindex><sitemap><loc>https://x/s1.xml</loc></sitemap></sitemapindex>'
    assert parse_sitemap(index) == ([], ["https://x/s1.xml"])
    assert parse_sitemap(gzip.compress(urlset)) == (["https://x/a"], [])
    assert parse_sitemap(b"not xml at all") == ([], [])


def test_sitemap_ignores_external_entities():
    evil = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>
    <urlset><url><loc>&e;</loc></url></urlset>"""
    pages, _ = parse_sitemap(evil)
    assert not any("root:" in p for p in pages)


def test_gzip_bomb_is_capped():
    bomb = gzip.compress(b"<urlset>" + b" " * (80 * 1024 * 1024) + b"</urlset>")
    pages, children = parse_sitemap(bomb)   # must return quickly without decompressing 80 MB
    assert pages == [] and children == []


def test_bad_gzip_and_bad_redirects_are_clean_fetch_errors():
    import asyncio

    import httpx

    from webatlas.config import CrawlConfig
    from webatlas.fetcher import Fetcher, FetchError

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path == "/gz":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"not gzip at all")
        return httpx.Response(302, headers={"location": "http://[::1"})

    async def go():
        cfg = CrawlConfig(per_host_delay=0, retries=3, allow_private_networks=True)
        async with Fetcher(cfg, transport=httpx.MockTransport(handler)) as f:
            with pytest.raises(FetchError, match="DecodingError"):
                await f.get("http://x.test/gz")
            assert calls["n"] == 1, "a decoding error must not be retried"
        cfg0 = CrawlConfig(per_host_delay=0, retries=0, allow_private_networks=True)
        async with Fetcher(cfg0, transport=httpx.MockTransport(handler)) as f:
            # httpx or our own check rejects it; either way it's a clean FetchError, not a crash
            with pytest.raises(FetchError, match="(?i)invalid (redirect|url)"):
                await f.get("http://x.test/redirect")

    asyncio.run(go())


def test_no_wasted_sleep_after_the_last_retry():
    import asyncio
    import time

    import httpx

    from webatlas.config import CrawlConfig
    from webatlas.fetcher import Fetcher, FetchError

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async def go():
        cfg = CrawlConfig(per_host_delay=0, retries=1, allow_private_networks=True)
        async with Fetcher(cfg, transport=httpx.MockTransport(handler)) as f:
            start = time.monotonic()
            with pytest.raises(FetchError):
                await f.get("http://x.test/")
            return time.monotonic() - start

    # one 1 s backoff between the two attempts, none after the final one
    assert asyncio.run(go()) < 1.6
