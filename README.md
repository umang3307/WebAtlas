# WebAtlas 🕸️

**A self-healing scraper that turns any organization's public document surface into an explorable knowledge graph.**

Built for [Into the Scrape-Verse](https://www.wemakedevs.org/hackathons/scrape-verse) — Bright Data Scraper Studio Hackathon, August 2026.

---

## 🌻 Team Details

- **Team Name:** Sunflowers
- **Team Members:**
  1. Umang Agarwal
  2. Aashvi Pandey

---

## What it does

Paste a URL — a college, school, or company's website — and WebAtlas automatically:

1. **Discovers** every internal page linked from that site using a Bright Data Scraper Studio collector
2. Lets you **select** which sections to crawl (or scan everything)
3. **Crawls** those sections, pulling out every downloadable document (PDFs, DOCs, XLS, etc.) it finds
4. **Builds a live knowledge graph** — Domain → Page → Document — with full provenance for every node
5. Renders it as an **interactive, explorable graph** with year-based clustering, search, and a directory-tree view

No content parsing or OCR — WebAtlas maps *what exists and how it's linked*, not what's inside each document. That keeps it fast and generalizable across any site's structure.

## Why it's different from just asking an LLM

- **Persistent, structured state** — every scan is stored in a graph database with provenance, not a one-off chat answer
- **Systematic discovery** — deterministically crawls every selected section, not "a few pages an agent thought to check"
- **Self-healing extraction** — when a scan comes back with no usable data, WebAtlas automatically triggers `bdata scraper heal` and `bdata scraper approve` on the collector and retries, without any manual intervention
- **A live, triggerable endpoint** — the Collector ID is wired directly into the app via Bright Data's `POST /dca/trigger` API, so any new domain becomes scannable on demand, no redeployment needed

## Self-healing, built into the product

Self-healing isn't just a terminal demo here — it's a real code path (`app.py` → `heal_collector()`):

- If a scan finds zero documents, the app automatically runs a heal prompt against the collector, re-approves it, and retries the crawl — all without a human in the loop
- If Bright Data reports a collector no longer exists (404), the app invalidates the cache and rebuilds it automatically
- Every scrape attempt — success, healed, or failed — is logged with a timestamp and shown live in the app's **Scrape Log** panel at the bottom of the UI

This means the resilience story isn't something we performed once for a recording — it's a standing feature of the pipeline.

## Architecture

```
URL input
│
▼
Discover sections  ──── Scraper Studio collector reads the homepage,
│                  returns every internal link + doc found there
▼
Section selection (user picks what to crawl)
│
▼
Crawler  ──────────── same collector, run per selected page via
│                 POST /dca/trigger → GET /dca/dataset
▼
Generic extractor  ─── recursively parses ANY JSON shape the collector
│                  returns (no hardcoded field names) into
│                  {document_url, title, date, page_url} records
▼
Knowledge graph (SQLite)  ── Domain/Page/Document nodes, LINKS_TO/
│                        HOSTED_ON edges, full scrape_log provenance
▼
Interactive graph UI  ── year-clustered, searchable, directory-tree view,
                         live Scrape Log panel
```

## Tech stack

Python, Flask, SQLite, vanilla JS + vis-network for the graph UI, Bright Data Scraper Studio (via the `bdata` CLI and REST API).

## Running it

```bash
pip install -r requirements.txt
npx -p @brightdata/cli bdata login
cp .env.example .env   # paste your Bright Data API token
python app.py
```

Open `http://localhost:5000`, paste a URL, click **Discover sections**, pick what to scan, click **Scan selected**.

## AI-use disclosure

This project was built with the assistance of an AI coding assistant (Claude) for scaffolding the backend architecture, database schema, extraction logic, and frontend. All core design decisions — including the self-healing flow, the graph model, and the crawl strategy — were made and understood by the team, who can explain each part of the codebase and the reasoning behind it.

## Scope & roadmap

- Document *content* parsing (OCR, structured field extraction) is deliberately out of scope for v1 — the graph is a map of what exists, not an analysis of what's inside each file
- Crawl depth and page count are capped for reliability and demo speed; both are configurable
- Future direction: content-level extraction, cross-document entity linking, scheduled re-scans with diffing to surface what changed over time
