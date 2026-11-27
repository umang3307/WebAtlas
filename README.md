# WebAtlas 🕸️

**A self-hosted crawler that finds every public document on a website and maps it as a knowledge graph.**

Point it at a college, school or company site. WebAtlas reads its `robots.txt` and sitemap, crawls the
site breadth-first, and records every PDF, Word, Excel, PowerPoint and archive file it links to, with the
page it was found on. No paid APIs, no API keys.

> **Status: v2 in progress.** This branch replaces the Bright Data hackathon version with a crawler
> WebAtlas owns end to end. See the [roadmap](#roadmap) for what's next.

---

## What's new in v2 (Phase 1)

| | v1 (hackathon) | v2 |
|---|---|---|
| Fetching pages | Bright Data Scraper Studio (paid, trial ended) | Own async crawler (`httpx`) |
| Finding pages | AI-generated collector per domain | `robots.txt` + `sitemap.xml` + breadth-first crawl |
| Following links | Selected pages only (`max_depth=0`) | Configurable depth and page budget |
| Spotting documents | File extension only | Extension, `Content-Type`, `Content-Disposition` and magic bytes |
| JavaScript sites | Handled by Bright Data | Optional headless-browser fallback (Playwright) |
| Security | User URL passed to a shell command | SSRF guard on every request and redirect, no shell calls, UI output escaped |
| Interfaces | Web UI | Web UI + `webatlas` command-line tool |
| Tests | None | 59 tests against a fake website (no network needed) |

## How it works

```
seed URL
  │
  ├─ SSRF guard ─────── refuse localhost, private and cloud-metadata addresses (every redirect hop too)
  ├─ robots.txt ─────── skip disallowed paths, honour Crawl-delay
  ├─ sitemap.xml ────── add pages no menu links to (gzip and sitemap indexes supported)
  ▼
priority queue (breadth-first, documents first)
  │
  ├─ page ──► fetch HTML ──► extract links ──► same-site pages  → queue (until max depth)
  │                                       └──► document links   → queue as "doc"
  │          (if the "page" is really a file, e.g. /download.php?id=5 → record it as a document)
  │
  └─ doc  ──► HEAD (or first 4 KB) ──► confirm by Content-Type / filename / magic bytes ──► record
  ▼
Domain → Page → Document graph (SQLite) ──► interactive graph UI
```

Politeness is built in: a per-host delay, a small number of concurrent requests, retries with backoff
that respect `Retry-After`, and size limits on every download.

## Quick start

```bash
git clone https://github.com/umang3307/WebAtlas.git
cd WebAtlas
pip install -e .
```

### Command line

```bash
webatlas crawl https://www.example.edu --depth 2 --max-pages 200 --out results.json
```

Useful options: `--download` (hash documents with SHA-256 and drop duplicates), `--render` (headless browser
for JavaScript-only pages), `--no-sitemap`, `--no-subdomains`, `--delay 1.0`. Run `webatlas crawl --help` for all.

### Web UI

```bash
python app.py
```

Open `http://localhost:5000`, paste a URL, click **Discover sections**, choose sections, click **Scan selected**.

### JavaScript-heavy sites (optional)

```bash
pip install -e ".[render]"
playwright install chromium
webatlas crawl https://www.example.edu --render
```

## Development

```bash
pip install -e ".[dev]"
pytest          # runs against a fake website, no network access needed
ruff check .
```

To try the crawler on a site running on your own machine, pass `--allow-private` to the CLI or set
`WEBATLAS_ALLOW_PRIVATE=1` for the web app. Both are off by default.

## Responsible use

Only scan sites you are allowed to scan. WebAtlas respects `robots.txt` and rate-limits itself by default;
keep it that way when crawling sites you don't own.

## Roadmap

- [x] **Phase 1:** own crawler: robots.txt, sitemaps, document detection, SSRF guard, CLI, tests
- [ ] **Phase 2:** FastAPI backend, resumable background jobs, live progress (SSE), SQLAlchemy storage
- [ ] **Phase 3:** document analysis: metadata leaks (authors, software, internal paths), emails, secrets
- [ ] **Phase 4:** AI layer: local-LLM link prioritisation, document classification, RAG Q&A with citations
- [ ] **Phase 5:** UI refresh, Docker Compose, benchmarks, demo

## History

v1 was built in August 2026 for [Into the Scrape-Verse](https://www.wemakedevs.org/hackathons/scrape-verse),
the Bright Data Scraper Studio hackathon, by team **Sunflowers** (Umang Agarwal and Aashvi Pandey). It used
Bright Data's AI-generated collectors and their heal API. That version is preserved on the `main` branch.

## AI-use disclosure

Built with the assistance of an AI coding assistant (Claude).
