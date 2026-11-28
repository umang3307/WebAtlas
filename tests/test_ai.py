"""Phase 4: the AI layer — client, embeddings, chunking, classification, indexing and Q&A.

No network and no model: the LLMClient talks to an in-process fake (tests/fake_llm.py), and the
offline embedder needs nothing at all.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from webatlas.ai import rag
from webatlas.ai.chunk import chunk_text
from webatlas.ai.classify import _parse, classify_document
from webatlas.ai.client import LLMUnavailable
from webatlas.ai.embeddings import Embedder, cosine, hashing_embed, pack, unpack
from webatlas.ai.indexer import AiIndexer
from webatlas.db import Database, DocumentRow

from .fake_llm import error_handler, make_client, unavailable_handler

# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "ai.db")
    return Database(path)


def _seed(db, scan_id="s1", docs=None):
    db.create_scan(scan_id, "http://x", ["http://x"], {}, "2026-01-01T00:00:00Z")
    db.set_status(scan_id, status="done", analysis_status="done", analyzed_docs=len(docs or []))
    rows = []
    for i, (url, title, dtype, text) in enumerate(docs or [], start=1):
        with db.engine.begin() as conn:
            conn.execute(DocumentRow.__table__.insert(),
                         {"id": i, "scan_id": scan_id, "url": url, "final_url": url,
                          "title": title, "doc_type": dtype})
        rows.append({"scan_id": scan_id, "document_id": i, "url": url, "title": title,
                     "doc_type": dtype, "text": text})
    db.store_content(rows)
    return scan_id


BUDGET = ("http://x/budget.pdf", "Budget 2025", "pdf",
          "The annual budget allocates two million dollars to road repair and public transit "
          "improvements across the city.\n\nA separate section covers staff salaries and pensions.")
POLICY = ("http://x/policy.docx", "Privacy Policy", "docx",
          "This privacy policy explains how we collect personal data and cookies from visitors. "
          "We retain personal data for five years before deletion.\n\nContact the data officer.")


# --------------------------------------------------------------------------- embeddings

def test_hashing_embed_is_deterministic_and_normalized():
    a = hashing_embed("the quick brown fox jumps")
    b = hashing_embed("the quick brown fox jumps")
    assert a == b
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6

def test_hashing_embed_stable_across_processes():
    # blake2b-based, so it must not depend on PYTHONHASHSEED. Compare with a subprocess value.
    import json
    import subprocess
    import sys
    code = ("from webatlas.ai.embeddings import hashing_embed;import json;"
            "print(json.dumps(hashing_embed('road repair budget')[:5]))")
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    assert json.loads(out) == hashing_embed("road repair budget")[:5]

def test_cosine_similar_text_scores_higher():
    q = hashing_embed("how much money for road repair")
    near = hashing_embed("the budget for road repair is two million")
    far = hashing_embed("privacy policy about personal data retention")
    assert cosine(q, near) > cosine(q, far)

def test_pack_unpack_roundtrip():
    v = hashing_embed("some text here")
    restored = unpack(pack(v))
    assert len(restored) == len(v)
    assert all(abs(a - b) < 1e-6 for a, b in zip(v, restored, strict=False))

def test_embedder_offline_when_no_client():
    emb = Embedder(client=None)
    vecs = emb.embed(["hello", "world"])
    assert len(vecs) == 2 and not emb.using_model

def test_embedder_uses_model_when_available():
    emb = Embedder(make_client())
    emb.embed(["hello"])
    assert emb.using_model

def test_embedder_falls_back_when_model_errors():
    emb = Embedder(make_client(error_handler))
    vecs = emb.embed(["hello"])
    assert len(vecs) == 1 and not emb.using_model   # offline fallback, no crash


# --------------------------------------------------------------------------- chunking

def test_chunk_short_text_single_chunk():
    chunks = chunk_text("one short paragraph.")
    assert chunks == ["one short paragraph."]

def test_chunk_respects_size():
    text = ". ".join(f"sentence number {i} with some filler words" for i in range(400))
    chunks = chunk_text(text, size=500)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)   # size + a little overlap slack

def test_chunk_empty_text():
    assert chunk_text("   ") == []

def test_chunk_hard_wraps_one_giant_token():
    chunks = chunk_text("x" * 5000, size=1000)
    assert len(chunks) >= 5


# --------------------------------------------------------------------------- client

def test_client_available_true_with_fake():
    assert make_client().available()

def test_client_available_false_when_unreachable():
    assert not make_client(unavailable_handler).available()

def test_client_chat_raises_unavailable_when_unreachable():
    with pytest.raises(LLMUnavailable):
        make_client(unavailable_handler).chat([{"role": "user", "content": "hi"}])

def test_client_embed_empty_returns_empty():
    assert make_client().embed([]) == []


# --------------------------------------------------------------------------- classification

def test_classify_parse_valid():
    cat, summ = _parse('{"category": "policy", "summary": "A privacy policy."}')
    assert cat == "policy" and summ == "A privacy policy."

def test_classify_parse_unknown_category_becomes_other():
    cat, _ = _parse('{"category": "banana", "summary": "x"}')
    assert cat == "other"

def test_classify_parse_handles_prose_wrapped_json():
    cat, _ = _parse('Sure! Here it is:\n{"category": "tender", "summary": "A tender."}\nHope that helps.')
    assert cat == "tender"

def test_classify_parse_garbage_returns_empty():
    assert _parse("not json at all") == ("", "")

def test_classify_document_with_model():
    cat, summ = classify_document(make_client(), "Budget", "pdf", "money and budgets")
    assert cat == "financial" and summ

def test_classify_document_empty_text():
    assert classify_document(make_client(), "", "pdf", "   ") == ("", "")


# --------------------------------------------------------------------------- indexer

def test_index_offline_builds_chunks(db):
    sid = _seed(db, docs=[BUDGET, POLICY])
    AiIndexer(db, sid, client=None, classify=False).run()
    scan = db.get_scan(sid)
    assert scan["ai_status"] == "done"
    assert scan["embed_kind"] == "offline"
    assert scan["chunks"] >= 2
    assert scan["indexed_docs"] == 2

def test_index_with_model_sets_kind_and_classifies(db):
    sid = _seed(db, docs=[BUDGET])
    AiIndexer(db, sid, client=make_client(), classify=True).run()
    scan = db.get_scan(sid)
    assert scan["embed_kind"] == "model"
    from sqlalchemy import select
    with db.engine.connect() as conn:
        cat = conn.execute(select(DocumentRow.category).where(DocumentRow.id == 1)).scalar()
    assert cat == "financial"

def test_index_no_content_is_error(db):
    db.create_scan("empty", "http://x", [], {}, "2026-01-01T00:00:00Z")
    AiIndexer(db, "empty", client=None).run()
    assert db.get_scan("empty")["ai_status"] == "error"

def test_reindex_clears_old_chunks(db):
    sid = _seed(db, docs=[BUDGET, POLICY])
    AiIndexer(db, sid, client=None, classify=False).run()
    first = db.get_scan(sid)["chunks"]
    AiIndexer(db, sid, client=None, classify=False).run()
    assert db.get_scan(sid)["chunks"] == first   # not doubled


# --------------------------------------------------------------------------- retrieval / answers

def test_retrieve_finds_relevant_document_offline(db):
    sid = _seed(db, docs=[BUDGET, POLICY])
    AiIndexer(db, sid, client=None, classify=False).run()
    hits = rag.retrieve(db, sid, "how much is allocated for road repair?", client=None)
    assert hits and "budget" in hits[0].url

def test_answer_retrieval_only_without_model(db):
    sid = _seed(db, docs=[BUDGET, POLICY])
    AiIndexer(db, sid, client=None, classify=False).run()
    ans = rag.answer(db, sid, "how long is personal data retained?", client=None)
    assert ans.mode == "retrieval_only"
    assert not ans.used_model
    assert ans.citations and "policy" in ans.citations[0].url

def test_answer_generated_with_model(db):
    sid = _seed(db, docs=[BUDGET])
    AiIndexer(db, sid, client=make_client(), classify=False).run()
    ans = rag.answer(db, sid, "how much for roads?", client=make_client())
    assert ans.mode == "generated"
    assert ans.used_model
    assert ans.citations

def test_answer_not_indexed(db):
    sid = _seed(db, docs=[BUDGET])   # analysed but never indexed
    ans = rag.answer(db, sid, "anything?", client=None)
    assert ans.mode == "not_indexed"

def test_answer_model_mismatch(db):
    # Index with a model, then query offline — the embeddings can't be compared.
    sid = _seed(db, docs=[BUDGET])
    AiIndexer(db, sid, client=make_client(), classify=False).run()
    ans = rag.answer(db, sid, "how much for roads?", client=None)
    assert ans.mode == "model_mismatch"

def test_answer_empty_question(db):
    sid = _seed(db, docs=[BUDGET])
    AiIndexer(db, sid, client=None, classify=False).run()
    ans = rag.answer(db, sid, "   ", client=None)
    assert ans.mode == "retrieval_only" and not ans.citations
