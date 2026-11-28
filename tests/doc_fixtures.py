"""Tiny real documents built with the standard library only, for analysis tests.

No third-party builders, so the tests have no extra dependencies: PDFs are assembled by hand and
Office/ODF files are zips written with `zipfile`.
"""

from __future__ import annotations

import io
import zipfile


def make_pdf(text: str, author: str = "Jane Doe", creator: str = "Microsoft Word 2019",
             producer: str = "Acme PDF 2.1") -> bytes:
    """A minimal one-page PDF with a text object and an Info dictionary. pypdf (strict=False)
    rebuilds the cross-reference table, so a hand-built xref isn't needed."""
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content = f"BT /F1 12 Tf 72 700 Td ({esc(text)}) Tj ET".encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Author ({esc(author)}) /Creator ({esc(creator)}) /Producer ({esc(producer)}) >>"
        .encode("latin-1", "replace"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF"
              % (len(objs) + 1, xref_pos))
    return out.getvalue()


_CORE = """<?xml version="1.0"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:creator>{author}</dc:creator>
  <cp:lastModifiedBy>{username}</cp:lastModifiedBy>
  <dc:title>{title}</dc:title>
</cp:coreProperties>"""

_APP = """<?xml version="1.0"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>{app}</Application><Company>{company}</Company>
</Properties>"""


def make_docx(text: str, author="Jane Doe", username="jdoe", company="Acme Corp",
              app="Microsoft Office Word", title="Internal Memo") -> bytes:
    doc = (f'<?xml version="1.0"?><w:document '
           f'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("docProps/core.xml", _CORE.format(author=author, username=username, title=title))
        z.writestr("docProps/app.xml", _APP.format(app=app, company=company))
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def make_xlsx(strings: list[str], author="Finance Bot", username="svc_finance",
              company="Acme Corp") -> bytes:
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared = (f'<?xml version="1.0"?><sst '
              f'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              f'count="{len(strings)}">{items}</sst>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("docProps/core.xml", _CORE.format(author=author, username=username, title="Sheet"))
        z.writestr("docProps/app.xml", _APP.format(app="Microsoft Excel", company=company))
        z.writestr("xl/sharedStrings.xml", shared)
    return buf.getvalue()


def make_zip_bomb(member: str = "word/document.xml") -> bytes:
    """A small file that claims a huge part, to test the zip-bomb guard."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("docProps/core.xml", _CORE.format(author="x", username="y", title="z"))
        z.writestr(member, b"A" * (40 * 1024 * 1024))   # 40 MB part, compresses tiny
    return buf.getvalue()
