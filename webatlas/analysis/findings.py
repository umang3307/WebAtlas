"""Turn an extracted document into findings: things a document leaks.

Each finding has a severity (critical > high > medium > low > info), a short title, where it
was found, and a detail. Secrets are **masked** before they are stored — WebAtlas flags that a
key leaked without itself keeping a copy of the key.

False positives are the enemy of a security tool, so the secret rules match specific provider
formats, and the one generic "key = value" rule skips obvious placeholders and short values.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from webatlas.analysis.extract import ExtractedDoc

SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
MAX_FINDINGS_PER_DOC = 100


@dataclass
class Finding:
    rule: str          # slug, e.g. "aws-access-key"
    severity: str
    title: str
    location: str      # "text", "metadata", or "metadata:<field>"
    detail: str        # always safe to display: secrets appear masked here
    count: int = 1
    secret: str = ""   # the full, unmasked value for secret findings; "" for everything else.
                       # Stored so the console can offer reveal/hide — masking is the default
                       # display, not a wall; the user decides whether to see the raw value.

    def sort_key(self) -> tuple[int, int]:
        return (_SEV_RANK.get(self.severity, 99), -self.count)


def severity_rank(severity: str) -> int:
    return _SEV_RANK.get(severity, 99)


# ---------------------------------------------------------------- secret rules

@dataclass(frozen=True)
class _Secret:
    rule: str
    severity: str
    title: str
    pattern: re.Pattern


def _p(rx: str) -> re.Pattern:
    return re.compile(rx)


_SECRETS: tuple[_Secret, ...] = (
    _Secret("private-key", "critical", "Private key",
            _p(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    _Secret("aws-access-key", "high", "AWS access key id", _p(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Secret("gcp-api-key", "high", "Google API key", _p(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    _Secret("github-pat", "critical", "GitHub personal access token",
            _p(r"\bghp_[0-9A-Za-z]{36}\b|\bgithub_pat_[0-9A-Za-z_]{59,}\b")),
    _Secret("gitlab-pat", "high", "GitLab personal access token", _p(r"\bglpat-[0-9A-Za-z_\-]{20}\b")),
    _Secret("slack-token", "high", "Slack token", _p(r"\bxox[baprs]-[0-9A-Za-z-]{10,48}\b")),
    _Secret("slack-webhook", "high", "Slack webhook URL",
            _p(r"https://hooks\.slack\.com/services/T[0-9A-Z]+/B[0-9A-Z]+/[0-9A-Za-z]+")),
    _Secret("stripe-key", "critical", "Stripe live secret key", _p(r"\b(?:sk|rk)_live_[0-9A-Za-z]{24,}\b")),
    _Secret("twilio-key", "high", "Twilio API key", _p(r"\bSK[0-9a-fA-F]{32}\b")),
    _Secret("sendgrid-key", "high", "SendGrid API key", _p(r"\bSG\.[0-9A-Za-z_\-]{22}\.[0-9A-Za-z_\-]{43}\b")),
    _Secret("openai-key", "high", "OpenAI API key", _p(r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b")),
    _Secret("jwt", "medium", "JSON Web Token",
            _p(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
)

# Generic "secret = value" — kept at medium and labelled "possible", with placeholder filtering.
_GENERIC_SECRET = _p(r"(?i)\b(password|passwd|pwd|secret|api[_\-]?key|access[_\-]?key|auth[_\-]?token|"
                     r"client[_\-]?secret|private[_\-]?key)\b\s*[:=]\s*[\"']?([^\s\"'<>]{6,100})")
# Tell-tale placeholder substrings (searched anywhere in the value) plus exact non-secrets.
_PLACEHOLDERS = re.compile(r"(?i)(x{3,}|\*{3,}|\.{4,}|changeme|placeholder|example|your[_\-]|"
                           r"secret[_\-]?here|redacted|removed|insert[_\-]|dummy|sample|<[a-z]|todo)")
_EXACT_NONSECRET = frozenset({"none", "null", "true", "false", "password", "secret", "n/a", "test123"})

# ---------------------------------------------------------------- other rules

_EMAIL = _p(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_EMAIL_NOISE = re.compile(r"(?i)@(example\.|w3\.org|openxmlformats|purl\.org|schemas\.|sentry\.|"
                          r"your(domain|company)|email\.com|domain\.com)")
_WIN_USER = _p(r"[A-Za-z]:\\Users\\[^\\\s\"'<>]+")
_WIN_OTHER = _p(r"[A-Za-z]:\\(?:inetpub|wwwroot|xampp|Program Files[^\\\s]*|Windows\\Temp)\\[^\s\"'<>]*")
_UNC = _p(r"\\\\[A-Za-z0-9._\-]+\\[^\s\"'<>]+")
_UNIX_HOME = _p(r"/(?:home|Users)/[A-Za-z0-9._\-]+/")
_CONFIDENTIAL = _p(r"(?i)\b(strictly confidential|confidential|internal use only|"
                   r"do not distribute|not for distribution|proprietary and confidential)\b")


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-2:]} ({len(value)} chars)"


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {c: value.count(c) for c in set(value)}
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ---------------------------------------------------------------- metadata detectors

_META_RULES = {
    "author": ("metadata-author", "low", "Author name in metadata"),
    "last_modified_by": ("metadata-username", "medium", "Internal username in metadata"),
    "company": ("metadata-company", "low", "Company name in metadata"),
    "manager": ("metadata-company", "low", "Manager name in metadata"),
}
_SOFTWARE_FIELDS = ("creator", "producer", "application", "app_version")


def _metadata_findings(meta: dict[str, str]) -> list[Finding]:
    found: list[Finding] = []
    for field, (rule, sev, title) in _META_RULES.items():
        value = meta.get(field)
        if value:
            found.append(Finding(rule, sev, title, f"metadata:{field}", value[:200]))
    software = [f"{k}={meta[k]}" for k in _SOFTWARE_FIELDS if meta.get(k)]
    if software:
        found.append(Finding("metadata-software", "low", "Software and version in metadata",
                             "metadata", "; ".join(software)[:300]))
    return found


# ---------------------------------------------------------------- content detectors

def _content_findings(text: str) -> list[Finding]:
    bucket: dict[tuple[str, str], Finding] = {}

    def add(rule: str, severity: str, title: str, detail: str, dedup: str, secret: str = "") -> None:
        key = (rule, dedup)
        if key in bucket:
            bucket[key].count += 1
        elif len(bucket) < MAX_FINDINGS_PER_DOC:
            bucket[key] = Finding(rule, severity, title, "text", detail, secret=secret)

    for spec in _SECRETS:
        for m in spec.pattern.finditer(text):
            value = m.group(0)
            add(spec.rule, spec.severity, spec.title, _mask(value), value, secret=value)

    for m in _GENERIC_SECRET.finditer(text):
        name, value = m.group(1), m.group(2)
        if (_PLACEHOLDERS.search(value) or value.lower() in _EXACT_NONSECRET
                or _entropy(value) < 2.0 or len(set(value)) < 4):
            continue   # looks like a placeholder or a dictionary word, not a real secret
        add("possible-secret", "medium", f"Possible hard-coded secret ({name.lower()})",
            f"{name} = {_mask(value)}", value, secret=f"{name} = {value}")

    for m in _EMAIL.finditer(text):
        email = m.group(0)
        if not _EMAIL_NOISE.search(email):
            add("email-address", "low", "Email address", email, email.lower())

    for rx, rule, sev, title in ((_WIN_USER, "internal-path", "medium", "Windows user path"),
                                 (_WIN_OTHER, "internal-path", "medium", "Server file path"),
                                 (_UNC, "internal-path", "medium", "Internal network share (UNC path)"),
                                 (_UNIX_HOME, "internal-path", "low", "Home directory path")):
        for m in rx.finditer(text):
            add(rule, sev, title, m.group(0)[:200], m.group(0).lower())

    for m in _CONFIDENTIAL.finditer(text):
        phrase = m.group(0)
        add("confidential-marker", "medium", "Marked confidential", phrase, phrase.lower())

    return list(bucket.values())


# ---------------------------------------------------------------- entry point

def scan_document(doc: ExtractedDoc) -> list[Finding]:
    """All findings for one extracted document, most severe first, capped per document."""
    findings = _metadata_findings(doc.metadata)
    # Secret/path/email rules run over the text and over metadata values (a path can hide there too).
    haystack = doc.text
    meta_blob = " ".join(doc.metadata.values())
    findings += _content_findings(haystack)
    if meta_blob.strip():
        for f in _content_findings(meta_blob):
            f.location = "metadata"
            findings.append(f)
    findings.sort(key=lambda f: f.sort_key())
    return findings[:MAX_FINDINGS_PER_DOC]


def summarize_severity(findings: list[Finding]) -> dict[str, int]:
    out = {s: 0 for s in SEVERITIES}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out
