"""Optional JavaScript rendering with Playwright.

Some sites build their menus with JavaScript, so the raw HTML has almost no
links. For those pages the crawler can load them in a headless Chromium and
read the HTML after scripts have run.

Install with:  pip install "webatlas[render]"  &&  playwright install chromium
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


class RenderUnavailable(Exception):
    pass


class Renderer:
    def __init__(self, user_agent: str, guard: Callable[[str], Awaitable[None]], timeout_ms: int = 20000):
        """`guard(url)` must raise for URLs the browser may not load (the same SSRF check
        the fetcher uses), so a page can't make the browser call internal addresses."""
        self.user_agent = user_agent
        self.guard = guard
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None

    async def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RenderUnavailable(
                'JavaScript rendering needs Playwright: pip install "webatlas[render]" '
                "&& playwright install chromium"
            ) from exc
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def render(self, url: str) -> str:
        """Return the page's HTML after JavaScript has run."""
        if self._browser is None:
            await self._start()
        context = await self._browser.new_context(user_agent=self.user_agent)
        try:
            page = await context.new_page()

            async def handle(route):
                request = route.request
                # Skip images, fonts and media - we only need the links.
                if request.resource_type in ("image", "media", "font"):
                    await route.abort()
                    return
                try:
                    await self.guard(request.url)
                except Exception:
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", handle)
            await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            return await page.content()
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
