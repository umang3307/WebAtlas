"""An in-process fake of an OpenAI-compatible server, for tests. No network.

Serves /models, /chat/completions and /embeddings via an httpx MockTransport, so the real
LLMClient code path (request building, parsing, error handling) is exercised end to end.
"""

from __future__ import annotations

import json

import httpx

from webatlas.ai.client import LLMClient, LLMConfig
from webatlas.ai.embeddings import hashing_embed


def _embedding(text: str) -> list[float]:
    # A real-ish embedding, distinct from the production offline path only by dimension,
    # so model-path tests retrieve sensibly.
    return hashing_embed(text, dim=64)


def _chat_reply(messages: list[dict]) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    if "label documents" in system:
        return json.dumps({"category": "financial", "summary": "A short budget summary."})
    # A generic grounded answer that cites the first source.
    return "Based on the sources, the answer is covered in the documents [1]."


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "GET" and path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
    body = json.loads(request.content or b"{}")
    if path.endswith("/chat/completions"):
        content = _chat_reply(body.get("messages", []))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    if path.endswith("/embeddings"):
        inputs = body.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [{"embedding": _embedding(t), "index": i} for i, t in enumerate(inputs)]
        return httpx.Response(200, json={"data": data})
    return httpx.Response(404, json={"error": "not found"})


def unavailable_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def error_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(200, json={"data": []})   # /models ok → available() True
    return httpx.Response(500, text="boom")


def make_client(handler_fn=handler) -> LLMClient:
    config = LLMConfig(url="http://fake/v1", model="fake-model", embed_model="fake-embed")
    return LLMClient(config, transport=httpx.MockTransport(handler_fn))
