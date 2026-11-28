"""Pull text and metadata out of a downloaded document.

Everything here parses **untrusted** files fetched from the web, so hardening matters as much
as correctness:

  - nothing over MAX_PARSE_BYTES is parsed at all;
  - zip-based files (docx/xlsx/pptx/odt) are checked for zip bombs before reading, and each part
    is read with a cap;
  - XML is parsed with external entities and network access off (no XXE, no billion-laughs);
  - PDFs are limited to MAX_PDF_PAGES;
  - extracted text is capped at MAX_TEXT_CHARS;
  - every parser is wrapped so a malformed or malicious file yields a partial result with an
    `error`, never an exception that reaches the caller.

Parsing is CPU-bound and can be slow on a hostile file, so callers run extract_document in a
worker thread (see webatlas.analyzer).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from lxml import etree

MAX_PARSE_BYTES = 30 * 1024 * 1024       # don't even try to parse a file bigger than this
MAX_TEXT_CHARS = 1_000_000                # scan at most this much extracted text
MAX_PDF_PAGES = 500
ZIP_MAX_MEMBERS = 2000
ZIP_MAX_TOTAL = 100 * 1024 * 1024         # total uncompressed size allowed across a zip
ZIP_MAX_PART = 30 * 1024 * 1024           # single part uncompressed cap

# External entities off, no network, recover from minor breakage, cap tree size (billion-laughs).
_XML = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


@dataclass
class ExtractedDoc:
    text: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    pages: int | None = None
    truncated: bool = False
    error: str = ""


def _clip(text: str) -> tuple[str, bool]:
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS], True
    return text, False


def extract_document(data: bytes, doc_type: str) -> ExtractedDoc:
    """Dispatch on doc_type. Always returns an ExtractedDoc, never raises."""
    if not data:
        return ExtractedDoc(error="empty file")
    if len(data) > MAX_PARSE_BYTES:
        return ExtractedDoc(error=f"file too large to analyse ({len(data)} bytes)")
    try:
        if doc_type == "pdf":
            return _pdf(data)
        if doc_type in ("docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"):
            return _zip_office(data, doc_type)
        if doc_type in ("doc", "xls", "ppt", "office-legacy"):
            return _legacy_office(data)
        if doc_type in ("txt", "csv", "rtf"):
            return _plain(data)
    except Exception as exc:   # defence in depth: no parser should escape, but if one does...
        return ExtractedDoc(error=f"could not parse {doc_type}: {type(exc).__name__}")
    return ExtractedDoc(error=f"no analyser for {doc_type}")


# ---------------------------------------------------------------- PDF

_PDF_META = {"/Author": "author", "/Creator": "creator", "/Producer": "producer",
             "/Title": "title", "/Subject": "subject", "/Keywords": "keywords",
             "/Company": "company", "/Manager": "manager"}


def _pdf(data: bytes) -> ExtractedDoc:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError  # base class for every pypdf error

    out = ExtractedDoc()
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except (PyPdfError, ValueError, OSError) as exc:
        return ExtractedDoc(error=f"could not read PDF: {type(exc).__name__}")

    if reader.is_encrypted:
        try:
            reader.decrypt("")   # many PDFs are "encrypted" with an empty owner password
        except Exception:
            out.error = "encrypted PDF (metadata only)"

    try:
        meta = reader.metadata or {}
        for key, name in _PDF_META.items():
            value = meta.get(key)
            if value:
                out.metadata[name] = str(value)[:500]
    except Exception:
        pass

    try:
        pages = reader.pages
        out.pages = len(pages)
        parts: list[str] = []
        total = 0
        for page in pages[:MAX_PDF_PAGES]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
            total += len(parts[-1])
            if total > MAX_TEXT_CHARS:
                break
        out.text, out.truncated = _clip("\n".join(parts))
    except Exception:
        if not out.error:
            out.error = "could not extract PDF text (metadata only)"
    return out


# ---------------------------------------------------------------- zip-based office

# core.xml / app.xml / meta.xml tags we care about, by local name -> our field name.
_OOXML_CORE = {"creator": "author", "lastModifiedBy": "last_modified_by", "title": "title",
               "subject": "subject", "keywords": "keywords", "description": "description",
               "category": "category", "revision": "revision"}
_OOXML_APP = {"Company": "company", "Manager": "manager", "Application": "application",
              "AppVersion": "app_version", "Template": "template"}
_ODF_META = {"initial-creator": "author", "creator": "last_modified_by", "generator": "application",
             "title": "title", "subject": "subject", "keyword": "keywords"}

# Which parts hold readable text, by document type.
_TEXT_PARTS = {
    "docx": ("word/document.xml",),
    "pptx": None,     # any ppt/slides/slideN.xml
    "xlsx": ("xl/sharedStrings.xml",),
    "odt": ("content.xml",), "ods": ("content.xml",), "odp": ("content.xml",),
    "epub": None,
}


def _safe_read(zf: zipfile.ZipFile, name: str, cap: int = ZIP_MAX_PART) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > cap:
        return None
    with zf.open(name) as fh:
        return fh.read(cap + 1)[:cap]


def _xml_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    root = etree.fromstring(blob, parser=_XML)
    if root is None:
        return ""
    return " ".join(t.strip() for t in root.itertext() if t and t.strip())


def _xml_fields(blob: bytes | None, wanted: dict[str, str], out: dict[str, str]) -> None:
    if not blob:
        return
    root = etree.fromstring(blob, parser=_XML)
    if root is None:
        return
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = el.tag.rsplit("}", 1)[-1]
        if local in wanted and el.text and el.text.strip():
            out.setdefault(wanted[local], el.text.strip()[:500])


def _zip_office(data: bytes, doc_type: str) -> ExtractedDoc:
    out = ExtractedDoc()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ExtractedDoc(error="not a valid Office/zip file")

    members = zf.infolist()
    if len(members) > ZIP_MAX_MEMBERS or sum(m.file_size for m in members) > ZIP_MAX_TOTAL:
        return ExtractedDoc(error="archive too large or too many parts to analyse safely")
    # Zip-bomb guard: a part that is both large and extremely compressible is refused outright.
    for m in members:
        ratio = m.file_size / m.compress_size if m.compress_size else 0
        if m.file_size > ZIP_MAX_PART and ratio > 50:
            return ExtractedDoc(error="archive looks like a zip bomb; not analysed")

    is_odf = doc_type in ("odt", "ods", "odp")
    try:
        if is_odf:
            _xml_fields(_safe_read(zf, "meta.xml"), _ODF_META, out.metadata)
        else:
            _xml_fields(_safe_read(zf, "docProps/core.xml"), _OOXML_CORE, out.metadata)
            _xml_fields(_safe_read(zf, "docProps/app.xml"), _OOXML_APP, out.metadata)
    except etree.XMLSyntaxError:
        pass

    parts = _TEXT_PARTS.get(doc_type)
    names: list[str]
    if parts is None and doc_type == "pptx":
        names = [m.filename for m in members if m.filename.startswith("ppt/slides/slide")
                 and m.filename.endswith(".xml")]
    elif parts is None:   # epub: all xhtml
        names = [m.filename for m in members if m.filename.endswith((".xhtml", ".html"))][:200]
    else:
        names = list(parts)

    chunks: list[str] = []
    total = 0
    for name in names:
        try:
            chunks.append(_xml_text(_safe_read(zf, name)))
        except etree.XMLSyntaxError:
            continue
        total += len(chunks[-1])
        if total > MAX_TEXT_CHARS:
            break
    out.text, out.truncated = _clip(" ".join(chunks))
    return out


# ---------------------------------------------------------------- legacy OLE2

# Standard OLE SummaryInformation property ids (MS-OLEPS).
def _legacy_office(data: bytes) -> ExtractedDoc:
    import olefile

    out = ExtractedDoc(error="legacy Office format (metadata only)")
    if not olefile.isOleFile(io.BytesIO(data)):
        return ExtractedDoc(error="not a valid legacy Office file")
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception:
        return ExtractedDoc(error="could not read legacy Office file")
    try:
        meta = ole.get_metadata()
        for attr, name in (("author", "author"), ("last_saved_by", "last_modified_by"),
                           ("company", "company"), ("manager", "manager"), ("title", "title"),
                           ("subject", "subject"), ("creating_application", "application"),
                           ("keywords", "keywords")):
            value = getattr(meta, attr, None)
            if value:
                text = value.decode("latin-1", "replace") if isinstance(value, bytes) else str(value)
                if text.strip():
                    out.metadata[name] = text.strip()[:500]
    except Exception:
        pass
    finally:
        ole.close()
    return out


# ---------------------------------------------------------------- plain text

def _plain(data: bytes) -> ExtractedDoc:
    text = data[:MAX_TEXT_CHARS * 2].decode("utf-8", "replace")
    clipped, truncated = _clip(text)
    return ExtractedDoc(text=clipped, truncated=truncated or len(data) > MAX_TEXT_CHARS * 2)
