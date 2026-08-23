"""
Automatically gets (or creates) a Scraper Studio collector for a given URL.
First time a domain is scanned, this runs `bdata scraper create` for it
(takes 5-15 min). Every scan after that reuses the cached collector ID
for that domain instantly.
"""
import subprocess
import re
import json
import os
from urllib.parse import urlparse

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "collectors_cache.json")

DESCRIPTION = (
    "For this page, return two lists. First, every internal navigation link "
    "to another page on this same website (full URL for each). Second, every "
    "downloadable document (PDF, DOC, DOCX, XLS, XLSX, PPT, CSV) linked on "
    "this page, with its title, direct file URL, and the date it was posted "
    "or last updated if shown."
)


def _load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_or_create_collector(url: str, status_cb=None) -> str:
    """Returns a Collector ID for the URL's domain, creating one via the
    bdata CLI if none is cached yet. status_cb(str) is called with
    progress messages if provided."""
    domain = urlparse(url).netloc
    cache = _load_cache()
    if domain in cache:
        if status_cb:
            status_cb(f"Reusing existing scraper for {domain} — will crawl the whole site with it")
        return cache[domain]

    if status_cb:
        status_cb(f"No scraper exists yet for {domain} — creating one now "
                   f"(this can take 5-15 minutes, please wait)...")

    cmd = f'npx -p @brightdata/cli bdata scraper create {url} "{DESCRIPTION}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1200, encoding="utf-8", errors="replace")
    output = (result.stdout or "") + (result.stderr or "")

    output_clean = re.sub(r"\x1b\[[0-9;]*m", "", output)  # strip ANSI color codes
    match = re.search(r"Collector\s*ID\s*[:\-]?\s*(c_[a-zA-Z0-9]+)", output_clean, re.IGNORECASE) \
        or re.search(r"\b(c_[a-zA-Z0-9]{8,})\b", output_clean)
    if not match:
        raise RuntimeError(f"Could not create a collector for {url}.\nCLI output:\n{output[-1500:]}")

    collector_id = match.group(1)
    cache[domain] = collector_id
    _save_cache(cache)
    if status_cb:
        status_cb(f"Scraper created for {domain}: {collector_id}")

    return collector_id

def heal_collector(collector_id: str, problem: str, status_cb=None) -> bool:
    """Runs `bdata scraper heal` then `bdata scraper approve` on an
    under-performing collector. Returns True if both steps succeeded."""
    if status_cb:
        status_cb(f"Healing collector {collector_id}...")

    heal_cmd = f'npx -p @brightdata/cli bdata scraper heal {collector_id} "{problem}"'
    heal_result = subprocess.run(heal_cmd, shell=True, capture_output=True, text=True,
                                  timeout=600, encoding="utf-8", errors="replace")
    heal_output = (heal_result.stdout or "") + (heal_result.stderr or "")
    if "healed" not in heal_output.lower():
        if status_cb:
            status_cb(f"Heal step did not confirm success for {collector_id}")
        return False

    if status_cb:
        status_cb(f"Approving heal for {collector_id}...")

    approve_cmd = f"npx -p @brightdata/cli bdata scraper approve {collector_id}"
    approve_result = subprocess.run(approve_cmd, shell=True, capture_output=True, text=True,
                                     timeout=300, encoding="utf-8", errors="replace")
    approve_output = (approve_result.stdout or "") + (approve_result.stderr or "")
    return approve_result.returncode == 0 and "healed" in approve_output.lower()