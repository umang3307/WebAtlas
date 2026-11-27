"""Command-line interface.

    webatlas crawl https://www.example.edu --depth 2 --max-pages 200 --out results.json
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from webatlas import __version__
from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.safety import UnsafeTargetError

app = typer.Typer(add_completion=False, help="WebAtlas: map every public document on a website.")
console = Console()


@app.callback()
def main() -> None:
    """WebAtlas command-line tool."""


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"WebAtlas {__version__}")


@app.command()
def crawl(
    url: list[str] = typer.Argument(..., help="One or more start URLs on the same site."),
    depth: int = typer.Option(2, "--depth", "-d", help="Link hops to follow from the start URLs."),
    max_pages: int = typer.Option(100, "--max-pages", "-n", help="Maximum HTML pages to fetch (0 = no limit)."),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="Requests in flight at once."),
    delay: float = typer.Option(0.5, "--delay", help="Minimum seconds between requests to one host."),
    sitemap: bool = typer.Option(True, "--sitemap/--no-sitemap", help="Read sitemap.xml."),
    subdomains: bool = typer.Option(True, "--subdomains/--no-subdomains", help="Include subdomains."),
    download: bool = typer.Option(False, "--download", help="Download documents to hash and dedupe them."),
    render: bool = typer.Option(False, "--render", help="Use a headless browser for JavaScript-only pages."),
    max_time: float = typer.Option(0, "--max-time", help="Stop after N seconds, keep results (0 = no limit)."),
    doc_checks: int = typer.Option(1000, "--doc-checks", help="Documents to confirm with a request (0 = no limit)."),
    unlimited: bool = typer.Option(False, "--unlimited", help="Lift depth, page, time and confirmation limits."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write full results to this JSON file."),
    allow_private: bool = typer.Option(False, "--allow-private", help="Allow localhost/private IPs (testing)."),
) -> None:
    """Crawl a site and list every document it links to."""
    no_limit = lambda n: None if unlimited or n <= 0 else n  # noqa: E731
    config = CrawlConfig(
        max_depth=None if unlimited else depth, max_pages=no_limit(max_pages),
        max_duration=no_limit(max_time), max_doc_checks=no_limit(doc_checks),
        concurrency=concurrency, per_host_delay=delay,
        use_sitemaps=sitemap, include_subdomains=subdomains, download_documents=download,
        render_js=render, allow_private_networks=allow_private,
    )
    lifted = [name for name, value in (("depth", config.max_depth), ("pages", config.max_pages),
                                       ("time", config.max_duration)) if value is None]
    if lifted:
        console.print(f"[yellow]No {', '.join(lifted)} limit: on a large site this can run for hours. "
                      "Press Ctrl+C to stop; results found so far are kept.[/yellow]")

    progress = Progress(
        TextColumn("[bold cyan]{task.description}"), BarColumn(), MofNCompleteColumn(),
        TextColumn("docs: [green]{task.fields[docs]}"), TimeElapsedColumn(),
        console=console, transient=True,
    )
    task = progress.add_task("Crawling", total=config.max_pages, docs=0)   # None = open-ended bar

    def on_event(event: dict) -> None:
        kind = event["type"]
        if kind == "page":
            progress.update(task, completed=event["pages_done"])
        elif kind == "document":
            progress.update(task, docs=event["documents_done"])
        elif kind == "info":
            progress.console.print(f"[dim]{event['message']}[/dim]")

    crawler = Crawler(config, on_event=on_event)
    try:
        with progress:
            result = _run_stoppable(crawler, url)
    except (UnsafeTargetError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _print_summary(result)
    if out:
        out.write_text(json.dumps(result.to_dict(), indent=2))
        console.print(f"\nFull results written to [bold]{out}[/bold]")


@app.command()
def ui(
    port: int = typer.Option(5000, "--port", "-p", help="Port for the web interface."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the interface in your browser."),
    db: Path = typer.Option(Path("webatlas.db"), "--db", help="SQLite file for scan history."),
) -> None:
    """Open the scan console in your browser."""
    import threading
    import webbrowser

    from werkzeug.serving import make_server

    from webatlas.web import create_app

    flask_app = create_app(db_path=db)
    # Bound to 127.0.0.1 on purpose: the console can start crawls, so it must not be reachable
    # from other machines. Werkzeug's threaded server is fine for one local user.
    server = make_server("127.0.0.1", port, flask_app, threaded=True)
    url = f"http://127.0.0.1:{port}"
    console.print(f"WebAtlas console running at [bold]{url}[/bold]  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Stopped.")


def _run_stoppable(crawler: Crawler, seeds: list[str]):
    """Run the crawl in a worker thread so Ctrl+C can stop it gracefully on every OS:
    the first Ctrl+C asks the crawler to finish and keeps its results, a second one quits."""
    import threading

    box: dict = {}

    def work() -> None:
        try:
            box["result"] = asyncio.run(crawler.crawl(seeds))
        except BaseException as exc:  # handed back to the main thread below
            box["error"] = exc

    thread = threading.Thread(target=work, name="webatlas-crawl", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.25)
    except KeyboardInterrupt:
        console.print("[yellow]Stopping... (press Ctrl+C again to quit without results)[/yellow]")
        crawler.request_stop()
        try:
            while thread.is_alive():
                thread.join(0.25)
        except KeyboardInterrupt:
            raise typer.Exit(code=130) from None
    if "error" in box:
        raise box["error"]
    return box["result"]


def _print_summary(result) -> None:
    console.print(
        f"\n[bold]Crawled {len(result.pages)} pages[/bold], found [bold green]{len(result.documents)} "
        f"documents[/bold green] ({len(result.failures)} failures, "
        f"{len(result.skipped_by_robots)} skipped by robots.txt, {result.sitemap_urls} URLs from sitemaps, "
        f"{result.requests} requests)"
    )
    if result.stopped_early:
        console.print(f"[yellow]Stopped early: {result.stopped_early}. Results so far are shown.[/yellow]")
    if result.failures and (not result.pages or len(result.failures) <= 5):
        for failure in result.failures[:5]:
            console.print(f"  [yellow]![/yellow] {failure.url}: {failure.reason}")
    if not result.documents:
        return
    types = Counter(d.doc_type for d in result.documents)
    console.print("By type: " + ", ".join(f"{t}: {n}" for t, n in types.most_common()))

    table = Table(show_lines=False, header_style="bold")
    table.add_column("Type", width=6)
    table.add_column("Title", overflow="fold", max_width=45)
    table.add_column("URL", overflow="fold")
    table.add_column("Found by", width=12)
    for doc in result.documents[:25]:
        table.add_row(doc.doc_type, doc.title, doc.final_url, doc.detected_by)
    console.print(table)
    if len(result.documents) > 25:
        console.print(f"[dim]... and {len(result.documents) - 25} more (use --out to save them all)[/dim]")


if __name__ == "__main__":
    app()
