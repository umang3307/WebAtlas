"""Passive vulnerability checks: observations about responses the crawl already fetched.

"Passive" is the point — nothing here sends a request of its own or probes for URLs that weren't
linked. It only reads the headers and body WebAtlas already has, so running it is free and can't
touch a target in a way the owner didn't invite. Active probing (requesting /.git, fuzzing, etc.)
belongs to a later, opt-in phase.

Findings are deduplicated by (rule, host) with a count, because a missing header is usually a
property of the server, not of one page — "no CSP on 240 pages of example.com" is one finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

SEVERITIES = ("critical", "high", "medium", "low", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Body is only skimmed for these cheap signals; cap how much we scan so a huge page can't stall us.
_BODY_SCAN_LIMIT = 200_000
_VERSION = re.compile(r"\d+\.\d+")
_DIR_LISTING = re.compile(r"<title>\s*(?:index of /|directory listing for )", re.I)
_HTTP_FORM = re.compile(r"<form\b[^>]*\baction\s*=\s*[\"']http://", re.I)
_PASSWORD_INPUT = re.compile(r"<input\b[^>]*\btype\s*=\s*[\"']password[\"']", re.I)
_MIXED = re.compile(r"<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)\s*=\s*[\"']http://", re.I)


@dataclass(frozen=True)
class SecFinding:
    rule: str
    severity: str
    title: str
    detail: str
    host: str = ""
    url: str = ""

    def sort_key(self) -> tuple[int, str]:
        return (_RANK.get(self.severity, len(SEVERITIES)), self.rule)


@dataclass
class _Rec:
    """A deduplicated security finding with its occurrence count and first example."""
    rule: str
    severity: str
    title: str
    detail: str
    host: str
    example_url: str
    count: int = 1


def scan_response(url: str, status: int, headers, content_type: str, body: bytes,
                  is_html: bool) -> list[SecFinding]:
    """Return the passive findings for one response. `headers` is anything with case-insensitive
    .get() and .get_list() (httpx.Headers); a plain dict also works for header presence."""
    split = urlsplit(url)
    host = split.netloc
    https = split.scheme == "https"
    out: list[SecFinding] = []

    def add(rule, severity, title, detail):
        out.append(SecFinding(rule, severity, title, detail, host=host, url=url))

    get = headers.get
    if not https and is_html and 200 <= status < 400:
        add("insecure-transport", "medium", "Page served over HTTP",
            "Served without TLS, so traffic can be read or modified in transit.")

    # Security response headers only make sense on an HTML document returned successfully.
    if is_html and 200 <= status < 300:
        if https and not get("strict-transport-security"):
            add("missing-hsts", "medium", "Missing Strict-Transport-Security",
                "No HSTS header, so browsers may fall back to HTTP.")
        if not get("content-security-policy"):
            add("missing-csp", "medium", "Missing Content-Security-Policy",
                "No CSP, so the page has no policy limiting script and resource origins.")
        if (get("x-content-type-options") or "").strip().lower() != "nosniff":
            add("missing-nosniff", "low", "Missing X-Content-Type-Options: nosniff",
                "Browsers may MIME-sniff the response and treat it as a different type.")
        csp = (get("content-security-policy") or "").lower()
        if not get("x-frame-options") and "frame-ancestors" not in csp:
            add("missing-frame-options", "low", "No clickjacking protection",
                "Neither X-Frame-Options nor CSP frame-ancestors is set.")
        if not get("referrer-policy"):
            add("missing-referrer-policy", "info", "Missing Referrer-Policy",
                "The full URL may leak to other sites via the Referer header.")

    _cookie_findings(headers, https, add)
    _disclosure_findings(get, add)
    if is_html and body:
        _body_findings(body, https, add)
    return out


def _cookie_findings(headers, https: bool, add) -> None:
    try:
        cookies = headers.get_list("set-cookie")
    except AttributeError:
        raw = headers.get("set-cookie")
        cookies = [raw] if raw else []
    for cookie in cookies:
        flags = cookie.lower()
        name = cookie.split("=", 1)[0].strip()
        if https and "secure" not in flags:
            add("cookie-no-secure", "medium", "Cookie without Secure flag",
                f"Cookie '{name}' can be sent over plain HTTP.")
        if "httponly" not in flags:
            add("cookie-no-httponly", "low", "Cookie without HttpOnly flag",
                f"Cookie '{name}' is readable by JavaScript.")
        if "samesite" not in flags:
            add("cookie-no-samesite", "low", "Cookie without SameSite attribute",
                f"Cookie '{name}' has no SameSite, weakening CSRF protection.")


def _disclosure_findings(get, add) -> None:
    server = get("server") or ""
    if _VERSION.search(server):
        add("server-version", "low", "Server version disclosed",
            f"Server header reveals software and version: {server[:80]}.")
    powered = get("x-powered-by")
    if powered:
        add("x-powered-by", "info", "Technology disclosed via X-Powered-By",
            f"X-Powered-By reveals the stack: {powered[:80]}.")


def _body_findings(body: bytes, https: bool, add) -> None:
    text = body[:_BODY_SCAN_LIMIT].decode("utf-8", errors="replace")
    if _DIR_LISTING.search(text):
        add("directory-listing", "medium", "Directory listing exposed",
            "The server returned an automatic index of files in a directory.")
    if _HTTP_FORM.search(text):
        add("form-insecure-action", "high", "Form submits over HTTP",
            "A form posts to an http:// URL, exposing submitted data in transit.")
    if not https and _PASSWORD_INPUT.search(text):
        add("password-over-http", "high", "Password field on an HTTP page",
            "A password input is served without TLS.")
    if https and _MIXED.search(text):
        add("mixed-content", "low", "Mixed content on an HTTPS page",
            "The page references script/style/image resources over http://.")


def summarize_severity(findings) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
