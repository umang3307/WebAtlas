import re
from urllib.parse import urlparse

DOC_EXTENSIONS = ("pdf", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "zip")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _same_site(url, root_netloc):
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    netloc = netloc[4:] if netloc.startswith("www.") else netloc
    root = root_netloc.lower()
    root = root[4:] if root.startswith("www.") else root
    return netloc == root or netloc.endswith("." + root)


def extract_links(node, root_netloc, found=None):
    """Recursively find URL-like strings that point to another page on the
    same site (or a subdomain of it) and are NOT documents — these become
    the crawl frontier."""
    if found is None:
        found = set()

    if isinstance(node, str):
        if URL_RE.match(node) and not _looks_like_doc_url(node) and _same_site(node, root_netloc):
            found.add(node.split("#")[0])
    elif isinstance(node, dict):
        for k, v in node.items():
            if k == "input":
                continue  # this is Scraper Studio's own request-echo, not a page link
            extract_links(v, root_netloc, found)
    elif isinstance(node, list):
        for item in node:
            extract_links(item, root_netloc, found)

    return found


def _looks_like_doc_url(value):
    if not isinstance(value, str) or not URL_RE.match(value):
        return False
    ext = value.split("?")[0].rsplit(".", 1)[-1].lower()
    return ext in DOC_EXTENSIONS


def _pick_field(d, keywords, exclude_key=None):
    for k, v in d.items():
        if k == exclude_key:
            continue
        if isinstance(v, str) and any(kw in k.lower() for kw in keywords):
            return v
    return None


def extract_documents(node, page_url_hint=None, found=None):
    """Recursively walk any dict/list shape and pull out document-like
    records based on URL file extensions, not fixed key names. This is
    what lets one collector generalize across differently-structured sites."""
    if found is None:
        found = []

    if isinstance(node, dict):
        doc_url_key = next((k for k, v in node.items() if _looks_like_doc_url(v)), None)
        if doc_url_key:
            title = _pick_field(node, ["title", "name", "label", "heading"], exclude_key=doc_url_key) or node[doc_url_key]
            date = _pick_field(node, ["date", "time", "published", "updated"])
            found.append({
                "document_url": node[doc_url_key],
                "title": title,
                "date": date,
                "page_url": page_url_hint,
            })

        new_hint = page_url_hint
        if isinstance(node.get("input"), dict) and node["input"].get("url"):
            new_hint = node["input"]["url"]

        for v in node.values():
            extract_documents(v, new_hint, found)

    elif isinstance(node, list):
        for item in node:
            extract_documents(item, page_url_hint, found)

    return found


def infer_file_type(url):
    if not url:
        return "unknown"
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return ext if len(ext) <= 5 else "unknown"
