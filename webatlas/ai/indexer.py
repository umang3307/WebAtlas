"""Build the AI index for a scan: classify documents and embed their text for question answering.

Runs after analysis (which stores the extracted text). Embedding uses the configured model when
one is reachable, otherwise the built-in offline embedder — so indexing always succeeds, with or
without a model. Classification needs a chat model and is skipped when none is available.

Synchronous; the web layer runs it in a worker thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from webatlas.ai.chunk import chunk_text
from webatlas.ai.classify import classify_document
from webatlas.ai.client import LLMClient
from webatlas.ai.embeddings import Embedder, pack
from webatlas.db import Database

FLUSH_EVERY = 20
MAX_CHUNKS_PER_DOC = 60      # guard against one pathological document flooding the index


class AiIndexer:
    def __init__(self, db: Database, scan_id: str, client: LLMClient | None = None, *,
                 classify: bool = True, max_duration: float | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.db = db
        self.scan_id = scan_id
        self.client = client
        self.classify = classify
        self.on_event = on_event or (lambda event: None)
        self._stop = False
        self._deadline = time.monotonic() + max_duration if max_duration else None
        self.indexed = 0
        self.chunk_count = 0

    def request_stop(self) -> None:
        self._stop = True

    def _out_of_time(self) -> bool:
        return self._stop or (self._deadline is not None and time.monotonic() >= self._deadline)

    def run(self) -> None:
        self.db.clear_ai(self.scan_id)
        self.db.set_status(self.scan_id, ai_status="running", ai_error="",
                           indexed_docs=0, chunks=0, embed_kind="")
        content = list(self.db.iter_content(self.scan_id))
        if not content:
            self.db.set_status(self.scan_id, ai_status="error",
                               ai_error="No analysed documents to index — run analysis first.")
            self.on_event({"type": "ai_done", "status": "error", "indexed": 0, "chunks": 0})
            return

        can_classify = self.classify and self.client is not None and self.client.available()
        embedder = Embedder(self.client)
        pending: list[dict] = []
        status = "done"

        for doc_id, url, title, doc_type, text in content:
            if self._out_of_time():
                status = "stopped"
                break
            if can_classify:
                category, summary = classify_document(self.client, title, doc_type, text)
                if category:
                    self.db.set_classification(doc_id, category, summary)
            chunks = chunk_text(text)[:MAX_CHUNKS_PER_DOC]
            if chunks:
                vectors = embedder.embed(chunks)
                for ordinal, (chunk, vec) in enumerate(zip(chunks, vectors, strict=False)):
                    pending.append({"scan_id": self.scan_id, "document_id": doc_id, "url": url,
                                    "title": title, "ordinal": ordinal, "text": chunk,
                                    "embedding": pack(vec)})
                self.chunk_count += len(chunks)
            self.indexed += 1
            if len(pending) >= 200 or self.indexed % FLUSH_EVERY == 0:
                self.db.add_chunks(pending)
                pending = []
                self.db.set_status(self.scan_id, indexed_docs=self.indexed, chunks=self.chunk_count)

        self.db.add_chunks(pending)
        embed_kind = "model" if embedder.using_model else "offline"
        self.db.set_status(self.scan_id, ai_status=status, indexed_docs=self.indexed,
                           chunks=self.chunk_count, embed_kind=embed_kind)
        self.on_event({"type": "ai_done", "status": status, "indexed": self.indexed,
                       "chunks": self.chunk_count, "embed_kind": embed_kind})
