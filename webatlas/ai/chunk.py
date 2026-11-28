"""Split document text into overlapping chunks for embedding.

Chunks break on paragraph then sentence boundaries where possible, so a retrieved passage reads as
a coherent unit rather than a mid-word slice.
"""

from __future__ import annotations

import re

CHUNK_CHARS = 1100
OVERLAP_CHARS = 150
_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?])\s+")


def _split_long(piece: str, size: int) -> list[str]:
    """Break a piece longer than `size` on sentence boundaries, then hard-wrap any remainder."""
    out: list[str] = []
    buf = ""
    for sent in _SENT.split(piece):
        if len(buf) + len(sent) + 1 <= size:
            buf = f"{buf} {sent}".strip()
        else:
            if buf:
                out.append(buf)
            if len(sent) <= size:
                buf = sent
            else:
                for i in range(0, len(sent), size):     # a single enormous "sentence"
                    out.append(sent[i:i + size])
                buf = ""
    if buf:
        out.append(buf)
    return out


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
        else:
            pieces.extend(_split_long(para, size))

    # Merge adjacent small pieces up to `size`, carrying a little overlap for context.
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if not cur:
            cur = piece
        elif len(cur) + len(piece) + 1 <= size:
            cur = f"{cur}\n{piece}"
        else:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap and len(cur) > overlap else ""
            cur = f"{tail}\n{piece}".strip() if tail else piece
    if cur:
        chunks.append(cur)
    return chunks
