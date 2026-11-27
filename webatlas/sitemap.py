"""sitemap.xml discovery and parsing.

Many sites list every page (and often their PDFs) in a sitemap, which finds
pages that no menu links to. Handles plain and gzipped sitemaps and sitemap
indexes (a sitemap that lists other sitemaps).
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from urllib.parse import urlsplit

from lxml import etree

from webatlas.fetcher import Fetcher, FetchError
from webatlas.robots import RobotsCache

SITEMAP_MAX_BYTES = 20 * 1024 * 1024
MAX_SITEMAP_FILES = 50            # sitemap files read when there's a URL limit
MAX_SITEMAP_FILES_UNLIMITED = 2000  # big sites split their sitemap into many files

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


def _gunzip_limited(data: bytes, limit: int = 50 * 1024 * 1024) -> bytes:
    """Decompress gzip but stop at `limit` bytes, so a tiny "zip bomb" can't eat all memory."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return decompressor.decompress(data, limit)


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from one sitemap file."""
    if xml[:2] == b"\x1f\x8b":
        try:
            xml = _gunzip_limited(xml)
        except zlib.error:
            return [], []
    try:
        root = etree.fromstring(xml, parser=_PARSER)
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    locs = [el.text.strip() for el in root.iter() if isinstance(el.tag, str)
            and el.tag.rsplit("}", 1)[-1] == "loc" and el.text and el.text.strip()]
    if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
        return [], locs
    return locs, []


async def collect_sitemap_urls(
    root_url: str,
    fetcher: Fetcher,
    robots: RobotsCache,
    limit: int | None,
    on_batch: Callable[[list[str]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    """Find the site's sitemaps (robots.txt `Sitemap:` lines, else /sitemap.xml) and
    return up to `limit` URLs listed in them (all of them if limit is None).

    `on_batch` receives the URLs of each sitemap file as soon as it's read, so a crawl can use
    them right away; `should_stop` is checked between files so Stop and time limits apply here too."""
    parts = urlsplit(root_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    queue = await robots.sitemaps(root_url) or [f"{origin}/sitemap.xml"]
    seen_files: set[str] = set()
    urls: list[str] = []
    max_files = MAX_SITEMAP_FILES if limit is not None else MAX_SITEMAP_FILES_UNLIMITED

    while queue and len(seen_files) < max_files and (limit is None or len(urls) < limit):
        if should_stop and should_stop():
            break
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_files:
            continue
        seen_files.add(sitemap_url)
        try:
            result = await fetcher.get(sitemap_url, max_bytes=SITEMAP_MAX_BYTES)
        except FetchError:
            continue
        if not result.ok:
            continue
        pages, children = parse_sitemap(result.body)
        batch = pages if limit is None else pages[: limit - len(urls)]
        urls.extend(batch)
        if on_batch and batch:
            on_batch(batch)
        queue.extend(children)
    return urls
