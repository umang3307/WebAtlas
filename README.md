# WebAtlas 🕸️

A self-healing scraper that turns any organization's public document
surface into an explorable knowledge graph — fully automated through
a web UI: paste a URL, watch it scrape, get a graph.

Built for [Into the Scrape-Verse](https://www.wemakedevs.org/hackathons/scrape-verse).

## How it works
1. Paste a root URL (e.g. `https://galgotiacollege.edu`)
2. Click **Discover sections** — WebAtlas creates (or reuses) a Bright Data
   Scraper Studio collector for the domain, runs it once against the homepage,
   and returns every internal page it linked to (notices, tenders, fee
   structures, etc.) — including any documents sitting right on the homepage
3. Pick which sections to actually crawl (or select all)
4. Click **Scan selected** — WebAtlas crawls exactly those sections (up to
   a page cap, default 25 pages) using the *same* collector for every page
5. A generic recursive extractor (`scraper/generic_extractor.py`) pulls
   documents *and* internal links out of whatever JSON shape the collector
   returns — no hardcoded field names, so one collector generalizes across
   differently-structured pages on the same site
6. Everything lands in a Domain → Page → Document knowledge graph (SQLite,
   full provenance) and renders live in the browser, documents clustered by year

No content parsing/OCR — this maps *what exists and how it's linked*, not
what's inside each document.

## Running it
```
pip install -r requirements.txt
npx -p @brightdata/cli bdata login
cp .env.example .env   # paste your Bright Data API token
python app.py
```
Open http://localhost:5000, paste a URL, click Scan.

## Self-healing
Bright Data's `bdata scraper heal <collector_id> "<what's wrong>"` was used
to refine the collector after an initial under-specified pass. See the
live "Self-heal log" panel in the app (bottom bar) for the recorded events.

## AI-use disclosure
Built with AI coding assistance (Claude) for initial scaffolding of the
Flask backend, SQLite schema, Bright Data API client, and the generic
JSON extractor pattern. From there, we tested it against real target sites,
debugged issues that only showed up under real crawls (including a crash
in the scan pipeline and a mismatch between the crawl depth the code
actually used vs. what we'd first documented), tuned the Scraper Studio
collector prompt through the self-heal workflow above, and wired up the
frontend polling/rendering UI. We can walk through and explain every part
of this codebase.

## Scope / roadmap
- No document content extraction (OCR/parsing) — deliberately out of scope
  to keep the tool fast and domain-agnostic; the graph is a map, not an analysis.
- Government sites are excluded per hackathon rules.

## Team — Sunflowers
- Umang Agarwal
- Aashvi Pandey
