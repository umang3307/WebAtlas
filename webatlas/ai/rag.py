"""Answer questions about a scan's documents, with citations.

Retrieval embeds the question with the *same* method the index was built with (model or offline),
finds the closest chunks by cosine similarity, and returns them. If a chat model is reachable, the
passages are handed to it to compose an answer that cites its sources; if not, WebAtlas returns the
passages themselves — document search still works with no model at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from webatlas.ai.client import LLMClient, LLMError
from webatlas.ai.embeddings import Embedder, cosine, unpack
from webatlas.db import Database

TOP_K = 6
MAX_CONTEXT_CHARS = 6000


@dataclass
class Citation:
    document_id: int
    url: str
    title: str
    ordinal: int
    snippet: str
    score: float


@dataclass
class Answer:
    mode: str                       # "generated" | "retrieval_only" | "not_indexed" | "model_mismatch"
    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    used_model: bool = False


def _query_embedder(embed_kind: str, client: LLMClient | None) -> Embedder | None:
    """The query must be embedded the same way as the index, or similarities are meaningless."""
    if embed_kind == "offline":
        return Embedder(force_offline=True)
    if embed_kind == "model":
        if client is None or not client.available():
            return None            # index needs the model, which isn't reachable now
        return Embedder(client)
    return None


def retrieve(db: Database, scan_id: str, question: str, client: LLMClient | None,
             k: int = TOP_K) -> list[Citation] | None:
    scan = db.get_scan(scan_id)
    if not scan or scan.get("ai_status") != "done":
        return None
    embedder = _query_embedder(scan.get("embed_kind", ""), client)
    if embedder is None:
        return []
    qvec = embedder.embed_one(question)
    scored: list[tuple[float, Citation]] = []
    for row in db.all_chunks(scan_id):
        cid, doc_id, url, title, ordinal, text, blob = row
        score = cosine(qvec, unpack(blob))
        snippet = text if len(text) <= 400 else text[:400] + "…"
        scored.append((score, Citation(doc_id, url, title, ordinal, snippet, round(score, 4))))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [c for _, c in scored[:k]]


def answer(db: Database, scan_id: str, question: str, client: LLMClient | None = None,
           k: int = TOP_K) -> Answer:
    question = (question or "").strip()
    if not question:
        return Answer(mode="retrieval_only", text="Ask a question about the documents.")
    scan = db.get_scan(scan_id)
    if not scan or scan.get("ai_status") != "done":
        return Answer(mode="not_indexed",
                      text="This scan has no AI index yet. Analyse its documents, then build the index.")
    if _query_embedder(scan.get("embed_kind", ""), client) is None:
        return Answer(mode="model_mismatch",
                      text="This index was built with an embedding model that isn't reachable now. "
                           "Start the model, or rebuild the index while offline.")

    hits = retrieve(db, scan_id, question, client, k) or []
    if not hits:
        return Answer(mode="retrieval_only", text="No relevant passages were found in the documents.")

    if client is not None and client.available():
        try:
            text = _compose(client, question, hits)
            return Answer(mode="generated", text=text, citations=hits, used_model=True)
        except LLMError:
            pass   # fall back to returning the passages
    passages = "\n\n".join(f"[{i + 1}] {c.title or c.url}\n{c.snippet}" for i, c in enumerate(hits))
    return Answer(mode="retrieval_only",
                  text="No answering model is configured, so here are the most relevant passages:\n\n"
                       + passages,
                  citations=hits)


def _compose(client: LLMClient, question: str, hits: list[Citation]) -> str:
    blocks, used = [], 0
    for i, c in enumerate(hits):
        block = f"[{i + 1}] (from {c.title or c.url})\n{c.snippet}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block)
    context = "\n\n".join(blocks)
    system = ("You answer questions using only the numbered sources provided. "
              "Cite the sources you use with bracketed numbers like [1]. "
              "If the sources don't contain the answer, say so plainly. Be concise.")
    user = f"Sources:\n{context}\n\nQuestion: {question}"
    return client.chat([{"role": "system", "content": system},
                        {"role": "user", "content": user}], temperature=0.1, max_tokens=600).strip()
