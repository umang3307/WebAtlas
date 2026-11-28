"""Phase 5: passive security scanning — the per-response checks and their integration."""

from __future__ import annotations

import httpx

from webatlas.passive import scan_response, summarize_severity

HTML = "text/html"


def rules(url, status, headers, body=b"<html></html>", is_html=True, content_type=HTML):
    hdrs = httpx.Headers(headers)
    return {f.rule for f in scan_response(url, status, hdrs, content_type, body, is_html)}


# --------------------------------------------------------------------------- headers

def test_https_page_missing_all_headers():
    got = rules("https://x.test/", 200, [("content-type", HTML)])
    assert {"missing-hsts", "missing-csp", "missing-nosniff", "missing-frame-options",
            "missing-referrer-policy"} <= got

def test_well_secured_page_is_clean():
    headers = [("content-type", HTML),
               ("strict-transport-security", "max-age=63072000"),
               ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
               ("x-content-type-options", "nosniff"),
               ("referrer-policy", "no-referrer")]
    assert rules("https://x.test/", 200, headers) == set()

def test_csp_frame_ancestors_satisfies_clickjacking():
    headers = [("content-type", HTML), ("content-security-policy", "frame-ancestors 'none'")]
    assert "missing-frame-options" not in rules("https://x.test/", 200, headers)

def test_http_page_flags_insecure_transport_not_hsts():
    got = rules("http://x.test/", 200, [("content-type", HTML)])
    assert "insecure-transport" in got
    assert "missing-hsts" not in got        # HSTS is meaningless on a plain-HTTP response

def test_headers_only_checked_on_html():
    # A JSON API response shouldn't be nagged about CSP/clickjacking.
    got = rules("https://x.test/api", 200, [("content-type", "application/json")],
                body=b"{}", is_html=False, content_type="application/json")
    assert "missing-csp" not in got and "missing-frame-options" not in got


# --------------------------------------------------------------------------- cookies

def test_insecure_cookie_flags():
    got = rules("https://x.test/", 200, [("content-type", HTML), ("set-cookie", "sid=abc; Path=/")])
    assert {"cookie-no-secure", "cookie-no-httponly", "cookie-no-samesite"} <= got

def test_secure_cookie_is_clean():
    got = rules("https://x.test/", 200,
                [("content-type", HTML), ("set-cookie", "sid=abc; Secure; HttpOnly; SameSite=Lax"),
                 ("strict-transport-security", "x"), ("content-security-policy", "frame-ancestors 'none'"),
                 ("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer")])
    assert not any(r.startswith("cookie-") for r in got)


# --------------------------------------------------------------------------- disclosure

def test_server_version_disclosure():
    assert "server-version" in rules("https://x.test/", 200,
                                     [("content-type", HTML), ("server", "Apache/2.4.41")])

def test_server_without_version_is_quiet():
    assert "server-version" not in rules("https://x.test/", 200,
                                        [("content-type", HTML), ("server", "cloudflare")])

def test_x_powered_by_disclosure():
    assert "x-powered-by" in rules("https://x.test/", 200,
                                   [("content-type", HTML), ("x-powered-by", "PHP/7.4")])


# --------------------------------------------------------------------------- body signals

def test_directory_listing():
    body = b"<html><head><title>Index of /files</title></head><body>...</body></html>"
    assert "directory-listing" in rules("https://x.test/files/", 200, [("content-type", HTML)], body=body)

def test_insecure_form_action():
    body = b'<form action="http://x.test/login" method="post"></form>'
    assert "form-insecure-action" in rules("https://x.test/", 200, [("content-type", HTML)], body=body)

def test_password_over_http():
    body = b'<form><input type="password" name="p"></form>'
    got = rules("http://x.test/login", 200, [("content-type", HTML)], body=body)
    assert "password-over-http" in got

def test_mixed_content_on_https():
    body = b'<script src="http://cdn.test/x.js"></script>'
    assert "mixed-content" in rules("https://x.test/", 200, [("content-type", HTML)], body=body)

def test_no_mixed_content_flag_on_http_page():
    body = b'<script src="http://cdn.test/x.js"></script>'
    assert "mixed-content" not in rules("http://x.test/", 200, [("content-type", HTML)], body=body)


# --------------------------------------------------------------------------- misc

def test_summarize_severity():
    findings = scan_response("https://x.test/", 200, httpx.Headers([("content-type", HTML),
                             ("set-cookie", "s=1")]), HTML, b"<html></html>", True)
    counts = summarize_severity(findings)
    assert counts["medium"] >= 1 and sum(counts.values()) == len(findings)

def test_plain_dict_headers_work():
    # scan_response should tolerate a plain dict (no get_list) for header presence.
    got = {f.rule for f in scan_response("https://x.test/", 200, {"content-type": HTML}, HTML,
                                         b"<html></html>", True)}
    assert "missing-csp" in got
