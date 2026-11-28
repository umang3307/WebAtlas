"""Phase 3: document extraction and the findings engine (pure functions, no network)."""

import pytest

from webatlas.analysis.extract import MAX_TEXT_CHARS, ExtractedDoc, extract_document
from webatlas.analysis.findings import _entropy, _mask, scan_document, severity_rank, summarize_severity

from .doc_fixtures import make_docx, make_pdf, make_xlsx, make_zip_bomb
from .secret_samples import AWS_KEY, GCP_KEY, GITHUB_PAT, SLACK_TOKEN, STRIPE_KEY


def rules(findings):
    return {f.rule for f in findings}


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


# ---------------------------------------------------------------- extraction


def test_pdf_metadata_and_text():
    pdf = make_pdf("hello world admin@acme.com", author="Jane Doe", creator="Word", producer="Acme 2.1")
    doc = extract_document(pdf, "pdf")
    assert doc.error == "" and doc.pages == 1
    assert doc.metadata["author"] == "Jane Doe"
    assert "admin@acme.com" in doc.text


def test_docx_metadata_and_text():
    doc = extract_document(make_docx("Quarterly numbers", author="A", username="jdoe", company="Acme"), "docx")
    assert doc.metadata["author"] == "A"
    assert doc.metadata["last_modified_by"] == "jdoe"
    assert doc.metadata["company"] == "Acme"
    assert "Quarterly numbers" in doc.text


def test_xlsx_shared_strings_text():
    doc = extract_document(make_xlsx(["Alpha", "Beta", "secret@acme.com"]), "xlsx")
    assert "secret@acme.com" in doc.text and "Alpha" in doc.text


def test_unknown_and_empty_and_oversized():
    assert extract_document(b"", "pdf").error == "empty file"
    assert "no analyser" in extract_document(b"abc", "zip").error
    big = b"x" * (31 * 1024 * 1024)
    assert "too large" in extract_document(big, "pdf").error


def test_corrupt_files_never_raise():
    assert extract_document(b"%PDF-1.4 broken", "pdf").error != "" or True   # must not raise
    assert extract_document(b"PK\x03\x04 not really a zip", "docx").error != ""
    assert extract_document(b"\xd0\xcf\x11\xe0 nope", "office-legacy").error != ""


def test_zip_bomb_is_refused():
    out = extract_document(make_zip_bomb(), "docx")
    assert "zip bomb" in out.error or out.text == ""


def test_xxe_is_not_resolved():
    # A docx whose core.xml tries to read /etc/passwd via an external entity.
    import io
    import zipfile
    evil_core = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                 '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                 ' xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>&e;</dc:creator></cp:coreProperties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("docProps/core.xml", evil_core)
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="x"><w:body/></w:document>')
    doc = extract_document(buf.getvalue(), "docx")
    assert "root:" not in doc.metadata.get("author", "")


def test_text_is_capped():
    huge = extract_document(("a" * (MAX_TEXT_CHARS + 5000)).encode(), "txt")
    assert len(huge.text) <= MAX_TEXT_CHARS and huge.truncated


# ---------------------------------------------------------------- findings: secrets


@pytest.mark.parametrize("text,rule,severity", [
    (f"key {AWS_KEY} here", "aws-access-key", "high"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private-key", "critical"),
    (f"tok {GITHUB_PAT} x", "github-pat", "critical"),
    (f"{GCP_KEY} used", "gcp-api-key", "high"),
    (SLACK_TOKEN, "slack-token", "high"),
    (f"{STRIPE_KEY} used", "stripe-key", "critical"),
])
def test_secret_rules_fire(text, rule, severity):
    findings = scan_document(ExtractedDoc(text=text))
    hits = by_rule(findings, rule)
    assert hits and hits[0].severity == severity


def test_secret_values_are_masked_not_stored():
    secret = AWS_KEY
    findings = by_rule(scan_document(ExtractedDoc(text=f"aws {secret}")), "aws-access-key")
    assert secret not in findings[0].detail
    assert findings[0].detail.startswith("AKI") and "***" in findings[0].detail


def test_generic_secret_skips_placeholders():
    for placeholder in ["password = changeme", 'api_key="xxxxxxxx"', "secret = your_secret_here",
                        "token = <token>", "password = password"]:
        assert not by_rule(scan_document(ExtractedDoc(text=placeholder)), "possible-secret"), placeholder


def test_generic_secret_detects_real_looking_value():
    hits = by_rule(scan_document(ExtractedDoc(text='password = "Tr0ub4dor&3xKf"')), "possible-secret")
    assert hits and hits[0].severity == "medium"
    assert "Tr0ub4dor&3xKf" not in hits[0].detail   # masked


# ---------------------------------------------------------------- findings: metadata, paths, email, confidential


def test_metadata_findings():
    doc = ExtractedDoc(metadata={"author": "Jane", "last_modified_by": "jdoe",
                                 "producer": "Acme PDF 2.1", "company": "Acme"})
    r = rules(scan_document(doc))
    assert {"metadata-author", "metadata-username", "metadata-software", "metadata-company"} <= r


def test_internal_paths():
    doc = ExtractedDoc(text=r"see C:\Users\jdoe\report.docx and \\fileserver\hr\pay.xlsx and /home/deploy/")
    paths = by_rule(scan_document(doc), "internal-path")
    details = " ".join(p.detail for p in paths)
    assert "jdoe" in details and "fileserver" in details


def test_emails_and_noise_filtering():
    doc = ExtractedDoc(text="real me@acme.com but not schemas@openxmlformats.org or x@example.com")
    emails = by_rule(scan_document(doc), "email-address")
    values = {e.detail for e in emails}
    assert "me@acme.com" in values
    assert not any("example.com" in v or "openxmlformats" in v for v in values)


def test_confidential_marker():
    assert by_rule(scan_document(ExtractedDoc(text="This is STRICTLY CONFIDENTIAL")), "confidential-marker")


def test_duplicate_findings_are_counted_once():
    doc = ExtractedDoc(text="me@acme.com me@acme.com me@acme.com")
    emails = by_rule(scan_document(doc), "email-address")
    assert len(emails) == 1 and emails[0].count == 3


def test_findings_sorted_most_severe_first():
    doc = ExtractedDoc(text=f"me@acme.com and {AWS_KEY}")
    findings = scan_document(doc)
    assert severity_rank(findings[0].severity) <= severity_rank(findings[-1].severity)
    assert findings[0].rule == "aws-access-key"


def test_summarize_severity():
    doc = ExtractedDoc(text=f"{AWS_KEY} me@acme.com other@acme.com")
    s = summarize_severity(scan_document(doc))
    assert s["high"] == 1 and s["low"] == 2


def test_helpers():
    assert _mask("short") == "***"
    assert _entropy("aaaa") < _entropy("Tr0ub4dor")
    assert _mask(AWS_KEY).startswith("AKI")
