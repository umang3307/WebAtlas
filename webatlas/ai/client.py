"""The LLM client: provider-agnostic, local-first, no API key required.

WebAtlas talks to any OpenAI-compatible endpoint — Ollama (the default), LM Studio, llama.cpp,
vLLM, or a hosted API if the operator chooses one. Configuration is entirely by environment
variable, never by the web UI, so a visitor to the console can't point WebAtlas at an arbitrary
URL:

  WEBATLAS_LLM_URL        base URL (default http://localhost:11434/v1, Ollama's OpenAI endpoint)
  WEBATLAS_LLM_MODEL      chat model          (default "llama3.1")
  WEBATLAS_LLM_EMBED_MODEL embedding model    (default "nomic-embed-text")
  WEBATLAS_LLM_KEY        API key, optional   (local models need none)

Everything degrades gracefully: if no model is reachable, chat features report "not configured"
and retrieval falls back to the built-in offline embedder (see embeddings.py), so document search
still works with no model at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

DEFAULT_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "llama3.1"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """The configured endpoint could not be reached."""


@dataclass
class LLMConfig:
    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL
    api_key: str = ""
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            url=os.environ.get("WEBATLAS_LLM_URL", DEFAULT_URL).rstrip("/"),
            model=os.environ.get("WEBATLAS_LLM_MODEL", DEFAULT_MODEL),
            embed_model=os.environ.get("WEBATLAS_LLM_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            api_key=os.environ.get("WEBATLAS_LLM_KEY", ""),
            timeout=float(os.environ.get("WEBATLAS_LLM_TIMEOUT", "60")),
        )


class LLMClient:
    """A thin OpenAI-compatible client. Synchronous (analysis already runs in worker threads)."""

    def __init__(self, config: LLMConfig | None = None, transport: httpx.BaseTransport | None = None):
        self.config = config or LLMConfig.from_env()
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._client = httpx.Client(base_url=self.config.url, headers=headers,
                                    timeout=self.config.timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def available(self) -> bool:
        """Quick reachability check; never raises."""
        try:
            resp = self._client.get("/models", timeout=5.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    def chat(self, messages: list[dict], *, temperature: float = 0.1, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        body: dict = {"model": self.config.model, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"could not reach the model at {self.config.url}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"model returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unexpected response from the model: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.config.embed_model, "input": texts}
        try:
            resp = self._client.post("/embeddings", json=body)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"could not reach the embedding model: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"embedding endpoint returned HTTP {resp.status_code}")
        try:
            data = resp.json()["data"]
            return [item["embedding"] for item in data]
        except (KeyError, ValueError) as exc:
            raise LLMError(f"unexpected embedding response: {exc}") from exc

    def info(self) -> dict:
        return {"url": self.config.url, "model": self.config.model,
                "embed_model": self.config.embed_model, "needs_key": bool(self.config.api_key)}
