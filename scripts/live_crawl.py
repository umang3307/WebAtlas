"""Crawl a few real public sites and print a summary. Used by .github/workflows/live-crawl.yml.

Small and polite on purpose: robots.txt respected, 1 s between requests per host,
a hard page budget.
"""

import asyncio
import json
import os
import sys
import time
from collections import Counter

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler

SITES = [
    "https://www.galgotiacollege.edu/",
    "https://www.iitb.ac.in/",
    "https://www.ugc.gov.in/",
    "https://www.aicte-india.org/",
    "https://www.cisa.gov/",
    "https://www.python.org/",
]


async def run(url: str, render: bool) -> dict:
    cfg = CrawlConfig(max_depth=2, max_pages=40, per_host_delay=1.0, concurrency=4, render_js=render,
                      max_duration=240, max_doc_checks=150)
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(Crawler(cfg).crawl([url]), timeout=600)
    except Exception as exc:  # report, don't crash the whole run
        return {"site": url, "render": render, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.monotonic() - start
    reasons = Counter(f.reason.split(":")[0][:60] for f in result.failures)
    out = {
        "site": url,
        "render": render,
        "seconds": round(elapsed, 1),
        "pages": len(result.pages),
        "rendered_pages": sum(p.rendered for p in result.pages),
        "documents": len(result.documents),
        "doc_types": dict(Counter(d.doc_type for d in result.documents)),
        "detected_by": dict(Counter(d.detected_by for d in result.documents)),
        "offsite_documents": sum(1 for d in result.documents if url.split("/")[2].replace("www.", "")
                                 not in d.final_url),
        "sitemap_urls": result.sitemap_urls,
        "robots_skipped": len(result.skipped_by_robots),
        "failures": len(result.failures),
        "requests": result.requests,
        "stopped_early": result.stopped_early,
        "failure_reasons": dict(reasons.most_common(5)),
        "sample_documents": [(d.doc_type, d.title[:60], d.final_url) for d in result.documents[:8]],
    }
    slug = url.split("/")[2] + ("-render" if render else "")
    os.makedirs("live-results", exist_ok=True)
    with open(f"live-results/{slug}.json", "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    return out


async def main() -> None:
    render = "--render" in sys.argv
    sites = [a for a in sys.argv[1:] if a.startswith("http")] or SITES
    for site in sites:
        summary = await run(site, render)
        print("LIVE_RESULT " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
