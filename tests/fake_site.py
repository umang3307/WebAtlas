"""A fake website served through httpx.MockTransport, so tests never use the network.

It models the things real college/company sites do that trip crawlers up:
robots.txt rules, a gzipped sitemap index, a PDF behind /download.php?id=5,
a server that rejects HEAD, a ".pdf" link that is really an HTML page,
duplicate links with tracking parameters, redirects, a flaky 503, and links
to other domains.
"""

from __future__ import annotations

import gzip
from collections import Counter

import httpx

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << >> endobj\n" + b"0" * 2000
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2000
XLSX = b"PK\x03\x04" + b"\x14\x00" * 10 + b"[Content_Types].xml" + b"xl/workbook.xml" + b"\x00" * 1000


def html(body: str, title: str = "Page") -> tuple[int, dict, bytes]:
    return 200, {"content-type": "text/html; charset=utf-8"}, (
        f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()
    )


def file(data: bytes, content_type: str, **headers: str) -> tuple[int, dict, bytes]:
    return 200, {"content-type": content_type, "content-length": str(len(data)), **headers}, data


SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>http://college.test/sitemap-pages.xml.gz</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://college.test/about</loc></url>
  <url><loc>http://college.test/files/prospectus-2026.pdf</loc></url>
  <url><loc>http://elsewhere.test/not-in-scope</loc></url>
</urlset>"""


class FakeSite:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []   # (method, url)
        self._flaky_calls = Counter()
        self.routes: dict[str, tuple[int, dict, bytes]] = {
            "http://college.test/robots.txt": (200, {"content-type": "text/plain"}, (
                b"User-agent: *\nDisallow: /private/\n\nSitemap: http://college.test/sitemap_index.xml\n")),
            "http://college.test/sitemap_index.xml": file(SITEMAP_INDEX, "application/xml"),
            "http://college.test/sitemap-pages.xml.gz": file(gzip.compress(SITEMAP_PAGES), "application/gzip"),
            "http://college.test/": html(
                '<a href="/notices">Notices</a> <a href="/tenders/">Tenders</a>'
                '<a href="/private/staff">Staff only</a> <a href="mailto:office@college.test">Mail</a>'
                '<a href="https://other.test/page">Partner</a>'
                '<a href="https://cdn.test/files/brochure.pdf">Brochure 2025</a>'
                '<a href="/old-notices">Old notices</a> <a href="/flaky">Results</a>'
                '<a href="#top">Top</a> <a href="javascript:void(0)">JS</a>',
                title="Fake College"),
            "http://college.test/notices": html(
                '<a href="/notices/circular-2024.pdf">Fee circular 2024</a>'
                '<a href="/notices/circular-2024.pdf?utm_source=newsletter">Fee circular (again)</a>'
                '<a href="/download.php?id=5">Exam schedule</a>'
                '<a href="/notices/old-rules.doc">Old rules</a>'
                '<a href="/notices/fake.pdf">Broken link</a>'
                '<a href="/notices/missing.pdf">Missing</a>',
                title="Notices"),
            "http://college.test/notices/circular-2024.pdf": file(
                PDF, "application/pdf", **{"last-modified": "Tue, 02 Jan 2024 10:00:00 GMT"}),
            "http://college.test/download.php?id=5": file(
                PDF, "application/octet-stream"),   # only the magic bytes say it's a PDF
            "http://college.test/notices/old-rules.doc": file(OLE2, "application/octet-stream"),
            "http://college.test/notices/fake.pdf": html("Please log in"),
            "http://college.test/tenders/": html('<a href="/tenders/page2">More tenders</a>', title="Tenders"),
            "http://college.test/tenders/page2": html(
                '<a href="/tenders/t-2023.xlsx">Tender sheet</a>', title="Tenders page 2"),
            "http://college.test/tenders/t-2023.xlsx": file(XLSX, "application/octet-stream"),
            "http://college.test/about": html('<a href="/files/annual-report.pdf">Annual report</a>', "About"),
            "http://college.test/files/annual-report.pdf": file(PDF, "application/pdf"),
            "http://college.test/files/prospectus-2026.pdf": file(PDF, "application/pdf"),
            "http://college.test/old-notices": (301, {"location": "/notices"}, b""),
            "http://college.test/private/staff": html("secret"),
            "https://cdn.test/files/brochure.pdf": file(PDF, "application/pdf"),
            "https://other.test/page": html('<a href="https://other.test/x.pdf">x</a>'),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url))
        if request.url.scheme == "https" and request.url.host == "college.test":
            raise httpx.ConnectError("connection refused (no TLS on this server)", request=request)

        if url == "http://college.test/flaky":
            self._flaky_calls[url] += 1
            if self._flaky_calls[url] == 1:
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b"<html><body><a href='/results/merit-list.pdf'>Merit</a></body></html>")
        if url == "http://college.test/results/merit-list.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF)

        if url not in self.routes:
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")
        status, headers, body = self.routes[url]
        if request.method == "HEAD":
            if url.endswith("old-rules.doc"):
                return httpx.Response(405)   # some servers don't support HEAD
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, headers=headers, content=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def fetched(self, url: str) -> bool:
        return any(u == url for _m, u in self.requests)
