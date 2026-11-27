"""Decide whether a URL or a response is a downloadable document.

Three signals, from cheapest to most reliable:
  1. the URL      - file extension in the path, or in a query value (?file=report.pdf)
  2. the headers  - Content-Type, or a file name in Content-Disposition
  3. the bytes    - "magic bytes" at the start of the file (%PDF-, PK.., OLE2 ...)

v1 only used (1), so it missed every PDF served from links like /download.php?id=123.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlsplit

# extension -> document type
DOC_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "doc": "doc", "docx": "docx", "odt": "odt", "rtf": "rtf",
    "xls": "xls", "xlsx": "xlsx", "ods": "ods", "csv": "csv",
    "ppt": "ppt", "pptx": "pptx", "odp": "odp",
    "txt": "txt", "epub": "epub",
    "zip": "zip", "rar": "rar", "7z": "7z",
}

# MIME type -> document type
DOC_MIME_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "application/x-pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "text/csv": "csv",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/epub+zip": "epub",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/vnd.rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
}

# Content types that say nothing about the file ("some bytes") - fall back to magic bytes.
GENERIC_MIME_TYPES = {"application/octet-stream", "binary/octet-stream", "application/force-download",
                      "application/download", "application/x-download", ""}

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # old Office files: .doc .xls .ppt
_ZIP = b"PK\x03\x04"                          # .docx .xlsx .pptx .odt .epub .zip


def _ext(name: str) -> str:
    name = unquote(name).lower().rstrip("/")
    if "." not in name.rsplit("/", 1)[-1]:
        return ""
    return name.rsplit(".", 1)[-1]


def doc_type_from_url(url: str) -> str | None:
    """'pdf' for https://x.edu/files/notice.pdf or https://x.edu/get?file=notice.pdf, else None."""
    parts = urlsplit(url)
    ext = _ext(parts.path)
    if ext in DOC_EXTENSIONS:
        return DOC_EXTENSIONS[ext]
    for _key, value in parse_qsl(parts.query):
        ext = _ext(value)
        if ext in DOC_EXTENSIONS:
            return DOC_EXTENSIONS[ext]
    return None


_FILENAME_RE = re.compile(r"filename\*?\s*=\s*(?:UTF-8''|\")?([^\";]+)", re.IGNORECASE)


def filename_from_disposition(header: str) -> str:
    match = _FILENAME_RE.search(header or "")
    return unquote(match.group(1)).strip() if match else ""


def doc_type_from_magic(first_bytes: bytes) -> str | None:
    head = first_bytes[:1024]
    if head.lstrip()[:5] == b"%PDF-":
        return "pdf"
    if head.startswith(_OLE2):
        return "office-legacy"     # .doc/.xls/.ppt - the container doesn't say which
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(_ZIP):
        # Office/OpenDocument files are zips; peek at the member names in the first entries.
        if b"word/" in first_bytes:
            return "docx"
        if b"xl/" in first_bytes:
            return "xlsx"
        if b"ppt/" in first_bytes:
            return "pptx"
        if b"mimetypeapplication/vnd.oasis.opendocument.text" in first_bytes:
            return "odt"
        if b"mimetypeapplication/epub+zip" in first_bytes:
            return "epub"
        return "zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    return None


def classify_response(url: str, content_type: str, content_disposition: str,
                      first_bytes: bytes) -> tuple[str, str] | None:
    """Return (doc_type, detected_by) if this response is a document, else None.
    HTML pages always return None."""
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type in ("text/html", "application/xhtml+xml"):
        return None
    if content_type in DOC_MIME_TYPES:
        return DOC_MIME_TYPES[content_type], "content-type"
    filename = filename_from_disposition(content_disposition)
    if filename and _ext(filename) in DOC_EXTENSIONS:
        return DOC_EXTENSIONS[_ext(filename)], "content-disposition"
    magic = doc_type_from_magic(first_bytes)
    if magic:
        return magic, "magic-bytes"
    if content_type in GENERIC_MIME_TYPES:
        by_url = doc_type_from_url(url)
        if by_url:
            return by_url, "extension"
    return None


def is_html(content_type: str, first_bytes: bytes) -> bool:
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type in ("text/html", "application/xhtml+xml"):
        return True
    if content_type in ("", "text/plain", "application/octet-stream"):
        sniff = first_bytes[:512].lstrip().lower()
        return sniff.startswith((b"<!doctype html", b"<html"))
    return False


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
