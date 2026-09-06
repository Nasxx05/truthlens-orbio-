"""Optional Playwright rendering.

Some retailers render reviews entirely client-side, so plain HTTP returns a
shell with no review markup in it. This module renders such pages in a real
browser and hands back the resulting HTML for the same extractors to work on.

Playwright is an optional dependency. When it is not installed, or no browser
is available, this degrades to returning nothing and the caller stays on the
HTTP path — a missing optional dependency must never fail a request.
"""

import asyncio
import logging
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Buttons that reveal more reviews, in the order we try them.
_MORE_BUTTON_SELECTORS = (
    'button:has-text("Load more")',
    'button:has-text("Show more reviews")',
    'button:has-text("More reviews")',
    'button:has-text("See more reviews")',
    'a:has-text("Load more")',
    '[data-hook="see-all-reviews-link-foot"]',
    'button[class*="load-more" i]',
    'button[class*="show-more" i]',
)

_REVIEW_HINT_SELECTORS = (
    '[data-hook="review"]',
    "[data-review-id]",
    '[class*="review-item" i]',
    '[class*="review-card" i]',
    '[itemtype*="Review" i]',
)


def available() -> bool:
    """Is Playwright importable?"""
    if not settings.playwright_enabled:
        return False
    try:
        import playwright.async_api  # noqa: F401

        return True
    except Exception:
        return False


async def render(url: str, *, click_more: int = 3) -> Optional[str]:
    """Render ``url`` in a headless browser and return its HTML.

    Clicks up to ``click_more`` "load more" style buttons to pull in reviews
    that are behind progressive disclosure. Returns ``None`` on any failure, so
    the caller can fall back rather than fail.
    """
    if not available():
        return None

    try:
        from playwright.async_api import async_playwright
    except Exception as error:
        logger.info("playwright import failed: %s", error)
        return None

    timeout_ms = int(settings.playwright_timeout * 1000)

    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as error:
                # Browser binary not installed: `playwright install chromium`.
                logger.info("chromium launch failed (%s); staying on HTTP path", error)
                return None

            try:
                context = await browser.new_context(
                    user_agent=settings.user_agent,
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                )
                page = await context.new_page()

                # Images and fonts are pure cost for text extraction.
                async def block_media(route):
                    if route.request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", block_media)
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

                # Give client-rendered reviews a chance to appear. One wait on
                # a combined selector, not one wait per pattern: waiting on
                # each in turn costs the full timeout times the pattern count
                # on a page that simply has no reviews.
                try:
                    await page.wait_for_selector(", ".join(_REVIEW_HINT_SELECTORS), timeout=4000)
                except Exception:
                    pass  # nothing matched; extract whatever rendered anyway

                await _reveal_more(page, click_more)
                return await page.content()
            finally:
                await browser.close()
    except Exception as error:
        logger.info("playwright render failed for %s: %s", url, error)
        return None


async def _reveal_more(page, rounds: int) -> None:
    """Scroll and click "load more" controls to pull in further reviews."""
    for _ in range(max(0, rounds)):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight * 0.8)")
            await asyncio.sleep(0.6)
        except Exception:
            return

        clicked = False
        for selector in _MORE_BUTTON_SELECTORS:
            try:
                button = page.locator(selector).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=2500)
                    await asyncio.sleep(1.2)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            return


__all__: List[str] = ["available", "render"]
