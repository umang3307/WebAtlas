"""URL clean-up and "is this the same site?" checks.

Two URLs that point to the same page should compare equal, otherwise the
crawler fetches the same page several times:

    HTTP://Example.com:80/a/../b/?b=2&a=1#top  ->  http://example.com/b/?a=1&b=2
"""

from __future__ import annotations

import posixpath
from urllib.parse import urljoin, urlsplit, urlunsplit

import tldextract
from w3lib.url import canonicalize_url

# Use the public-suffix list bundled with tldextract so nothing is downloaded at runtime.
# include_psl_private_domains=True keeps "someone.github.io" separate from "other.github.io".
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Query parameters that only track visitors and never change the content.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def is_http_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def _remove_dot_segments(path: str) -> str:
    if not path:
        return "/"
    trailing = path.endswith("/")
    cleaned = posixpath.normpath(path)
    if cleaned == ".":
        cleaned = "/"
    if cleaned.startswith("//"):
        cleaned = "/" + cleaned.lstrip("/")
    if trailing and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return a canonical absolute http(s) URL, or None if the link is not crawlable
    (mailto:, javascript:, tel:, data:, empty, ...)."""
    if url is None:
        return None
    url = url.strip()
    if not url or url.startswith("#"):
        return None
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:   # e.g. "http://[::1/" - malformed IPv6 host
            return None
    if not is_http_url(url):
        return None

    try:
        parts = urlsplit(url)   # raises ValueError for a malformed IPv6 host
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port       # raises ValueError for junk like "http://host:abc/"
    except ValueError:
        return None
    if not host:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS.get(parts.scheme)) else f"{host}:{port}"

    query = "&".join(
        kv for kv in parts.query.split("&") if kv and not kv.lower().startswith(_TRACKING_PARAMS)
    )
    rebuilt = urlunsplit((parts.scheme.lower(), netloc, _remove_dot_segments(parts.path), query, ""))
    # canonicalize_url sorts query parameters and fixes percent-encoding.
    return canonicalize_url(rebuilt, keep_fragments=False)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def registered_domain(url_or_host: str) -> str:
    """example.ac.in for https://admissions.cs.example.ac.in/x. Falls back to the
    bare host for IPs and single-label hosts like localhost."""
    host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
    ext = _EXTRACT(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def in_scope(url: str, root_url: str, include_subdomains: bool = True) -> bool:
    """Is `url` part of the site that `root_url` belongs to?"""
    host, root_host = host_of(url), host_of(root_url)
    if not host or not root_host:
        return False
    strip_www = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    if strip_www(host) == strip_www(root_host):
        return True
    if include_subdomains:
        return registered_domain(host) == registered_domain(root_host)
    return False


def file_name(url: str) -> str:
    """Last path segment, used as a fallback document title."""
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url
