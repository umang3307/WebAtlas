"""Pull links and the title out of an HTML page."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from webatlas.models import Link
from webatlas.urls import normalize_url

# Tags and attributes that can point at another page or a file.
_LINK_ATTRS = (
    ("a", "href"),
    ("area", "href"),
    ("iframe", "src"),
    ("frame", "src"),
    ("embed", "src"),
    ("object", "data"),
    ("link", "href"),   # only rel=alternate / rel=next etc. — stylesheets are filtered below
)
_SKIP_LINK_RELS = {"stylesheet", "icon", "shortcut", "apple-touch-icon", "preload", "prefetch",
                   "dns-prefetch", "preconnect", "manifest", "modulepreload"}
_WS = re.compile(r"\s+")
_META_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()[:300]


def extract_page(html: str | bytes, page_url: str, encoding: str | None = None) -> tuple[str, list[Link]]:
    """Return (page_title, links). Links are absolute, normalized and de-duplicated;
    the first non-empty anchor text seen for a URL is kept as its text.

    Pass raw bytes when you can: with no `encoding` (the HTTP header's charset), the parser
    reads the page's own <meta charset>, so pages in windows-1252, Shift_JIS etc. aren't garbled.
    This is CPU work, so async callers should run it in a thread."""
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "lxml", from_encoding=encoding)
    else:
        soup = BeautifulSoup(html, "lxml")

    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = normalize_url(base_tag["href"], page_url) or page_url

    title = _clean(soup.title.get_text()) if soup.title else ""
    found: dict[str, str] = {}

    def add(raw: str | None, text: str = "") -> None:
        url = normalize_url(raw or "", base)
        if not url:
            return
        if url not in found or (not found[url] and text):
            found[url] = text

    for tag_name, attr in _LINK_ATTRS:
        for tag in soup.find_all(tag_name):
            if tag_name == "link":
                rels = {r.lower() for r in (tag.get("rel") or [])}
                if not rels or rels & _SKIP_LINK_RELS:
                    continue
            text = _clean(tag.get_text(" ")) or _clean(tag.get("title", "")) or _clean(tag.get("aria-label", ""))
            add(tag.get(attr), text)

    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)}):
        match = _META_REFRESH_URL.search(meta.get("content", ""))
        if match:
            add(match.group(1))

    return title, [Link(url=u, text=t) for u, t in found.items()]
