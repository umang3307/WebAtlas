"""Scan history in SQLite.

One row per scan: its settings, a short summary for the history list, and the
full result as JSON. Phase 2 replaces this with SQLAlchemy models.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("WEBATLAS_DB", "webatlas.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id           TEXT PRIMARY KEY,
    target       TEXT NOT NULL,
    status       TEXT NOT NULL,          -- running | done | stopped | error | interrupted
    settings     TEXT NOT NULL,          -- JSON
    summary      TEXT NOT NULL,          -- JSON: counts for the history list
    result       TEXT,                   -- JSON: full CrawlResult, when finished
    error        TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_started ON scans(started_at DESC);
"""


class ScanStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            # WAL lets the interface read history while a finishing scan writes its results.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Scans that were running when the server stopped can't be resumed (yet).
            conn.execute("UPDATE scans SET status = 'interrupted' WHERE status = 'running'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error, and is always closed.
        (sqlite3's own context manager commits but leaves the connection open.)"""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, scan_id: str, target: str, settings: dict, started_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO scans (id, target, status, settings, summary, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, target, "running", json.dumps(settings), "{}", started_at),
            )

    def finish(self, scan_id: str, status: str, summary: dict, result: dict | None,
               error: str | None, finished_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET status = ?, summary = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, json.dumps(summary), json.dumps(result) if result is not None else None,
                 error, finished_at, scan_id),
            )

    def list(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, target, status, summary, started_at, finished_at FROM scans "
                "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]

    def get(self, scan_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["settings"] = json.loads(data["settings"] or "{}")
        data["summary"] = json.loads(data["summary"] or "{}")
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def delete(self, scan_id: str) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,)).rowcount > 0
