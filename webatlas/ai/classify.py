"""Classify a document with the LLM: a category plus a one-line summary.

Needs a chat model; with none configured, classification is simply skipped (see the indexer).
Never raises — on any model error it returns ("", "").
"""

from __future__ import annotations

import json

from webatlas.ai.client import LLMClient, LLMError

CATEGORIES = ("notice", "tender", "policy", "report", "financial", "form",
              "legal", "correspondence", "technical", "academic", "other")

_SYSTEM = (
    "You label documents found while crawling an organisation's website. "
    "Reply with a single JSON object and nothing else: "
    '{"category": "<one of: ' + ", ".join(CATEGORIES) + '>", "summary": "<one sentence, <=30 words>"}. '
    "Base the label only on the text given; if unsure use \"other\"."
)

MAX_INPUT_CHARS = 6000
MAX_SUMMARY_CHARS = 300


def classify_document(client: LLMClient, title: str, doc_type: str, text: str) -> tuple[str, str]:
    sample = text[:MAX_INPUT_CHARS].strip()
    if not sample:
        return "", ""
    header = f"Filename/title: {title or '(none)'}\nFile type: {doc_type}\n\n" if (title or doc_type) else ""
    user = f"{header}Document text:\n{sample}"
    try:
        raw = client.chat([{"role": "system", "content": _SYSTEM},
                           {"role": "user", "content": user}],
                          temperature=0.0, max_tokens=180, json_mode=True)
    except LLMError:
        return "", ""
    return _parse(raw)


def _parse(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    # Some models wrap JSON in prose or code fences; pull out the first {...} block.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return "", ""
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(obj, dict):
        return "", ""
    category = str(obj.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        category = "other"
    summary = str(obj.get("summary", "")).strip()[:MAX_SUMMARY_CHARS]
    return category, summary
