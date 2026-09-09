"""Host-site review scraping.

The host site is the one the shopper is on — the site they intend to buy from.
This module is the only entry point the route needs:

    result = await scrape_host_reviews(product_url, product_id, site_hint)

It picks the site adapter, walks review pages within a page and wall-clock
budget, and returns a ``ScrapeResult`` that always describes what happened.
Nothing here raises: a source that fails reports the failure so the endpoint
can return partial results instead of an error.
"""

import logging
import time
from typing import List, Optional
from urllib.parse import urlparse

from app.config import settings
from app.services.scrapers import browser
from app.services.scrapers.base import Review, ScrapeResult
from app.services.scrapers.extract import (
    extract_product_description,
    extract_product_details,
    extract_product_image,
    from_dom,
    from_structured_data,
)
from app.services.scrapers.fetch import Fetcher
from app.services.scrapers.sites import adapter_for

logger = logging.getLogger(__name__)


def _source_name(url: str, site_hint: Optional[str]) -> str:
    """Label reviews with the platform they came from.

    Attribution matters: the point of the project is comparing platforms, so
    reviews must never become an anonymous pool.
    """
    if site_hint:
        return site_hint
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host or "host"
    except Exception:
        return "host"


def _extract(html: str, source: str, page_url: str) -> tuple:
    """Run both extractors over one page. Returns ``(reviews, strategy_label)``.

    Structured data wins when present — the site labelled those as reviews
    itself. The DOM heuristic usually finds more, since most retailers publish
    only an aggregate rating as structured data.
    """
    structured: List[Review] = []
    dom: List[Review] = []

    try:
        structured = from_structured_data(html, source, page_url)
    except Exception as error:
        logger.debug("structured extraction failed on %s: %s", page_url, error)

    try:
        dom = from_dom(html, source, page_url)
    except Exception as error:
        logger.debug("dom extraction failed on %s: %s", page_url, error)

    if len(dom) > len(structured):
        return dom, "dom"
    if structured:
        return structured, "structured-data"
    return dom, "dom"


async def scrape_host_reviews(
    product_url: str,
    product_id: Optional[str] = None,
    site_hint: Optional[str] = None,
    limit: Optional[int] = None,
) -> ScrapeResult:
    """Scrape reviews for one product from the host site.

    Walks candidate review URLs, then paginates from whichever one produced
    reviews, until the review cap, the page cap, or the wall-clock budget is
    reached.
    """
    started = time.monotonic()
    limit = limit or settings.review_max
    source = _source_name(product_url, site_hint)
    result = ScrapeResult(source=source)

    if not product_url or not product_url.startswith(("http://", "https://")):
        result.ok = False
        result.error = "no usable product URL to scrape"
        result.notes.append("Host scraping needs a product URL; a name alone is not enough.")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    adapter = adapter_for(product_url, site_hint)
    result.notes.append(f"adapter: {adapter.name}")

    def budget_left() -> float:
        return settings.total_timeout - (time.monotonic() - started)

    try:
        async with Fetcher(referer=product_url) as fetcher:
            candidates = adapter.review_urls(product_url, product_id)
            paginating_from: Optional[str] = None
            last_html: Optional[str] = None

            # --- Pass 1: the candidate entry points -----------------------
            for candidate in candidates:
                if len(result.reviews) >= limit or budget_left() <= 2:
                    break

                page = await fetcher.get(candidate)
                result.pages_fetched += 1

                if page.blocked:
                    result.blocked = True
                    # robots.txt refusals and bot walls are different problems
                    # with the same effect; say which one happened.
                    result.notes.append(f"{candidate} -> {page.error}")
                    continue
                if not page.ok or not page.html:
                    result.notes.append(f"{candidate} -> {page.error or 'no content'}")
                    # An unreachable host will not become reachable for the
                    # next candidate URL; stop rather than burning the budget
                    # on retries against a dead server.
                    if page.transport_error:
                        result.notes.append("host unreachable; skipping remaining candidates")
                        break
                    continue

                if result.image_url is None:
                    try:
                        result.image_url = extract_product_image(page.html, page.url)
                    except Exception as error:
                        logger.debug("product image extraction failed on %s: %s", page.url, error)

                if result.description is None:
                    try:
                        result.description = extract_product_description(page.html, page.url)
                    except Exception as error:
                        logger.debug("product description extraction failed on %s: %s", page.url, error)

                if not result.product_details:
                    try:
                        result.product_details = extract_product_details(page.html, page.url)
                    except Exception as error:
                        logger.debug("product detail extraction failed on %s: %s", page.url, error)

                reviews, strategy = _extract(page.html, source, page.url)
                added = result.add(reviews, strategy, limit)
                logger.info("%s: %s review(s) via %s from %s", source, added, strategy, candidate)

                if added and paginating_from is None:
                    paginating_from = page.url
                    result.notes.append(f"paginating from {page.url}")
                    # Keep the HTML of the page we will paginate from.
                    last_html = page.html
                    # The remaining candidates are alternate locations for the
                    # same reviews, not additional ones. Fetching them now
                    # would just 404 or duplicate; pagination continues below.
                    break

            # --- Pass 2: pagination ---------------------------------------
            if paginating_from and last_html and len(result.reviews) < limit:
                page_url = paginating_from
                html = last_html
                page_number = 1

                while (
                    page_number < settings.max_pages
                    and len(result.reviews) < limit
                    and budget_left() > 3
                ):
                    try:
                        next_url = adapter.next_page(html, page_url, page_number)
                    except Exception as error:
                        logger.debug("next_page failed: %s", error)
                        next_url = None

                    if not next_url or next_url == page_url:
                        break

                    page = await fetcher.get(next_url)
                    result.pages_fetched += 1

                    if page.blocked:
                        result.blocked = True
                        result.notes.append(f"pagination stopped: {page.error}")
                        break
                    if not page.ok or not page.html:
                        result.notes.append(f"pagination stopped at page {page_number + 1}")
                        break

                    reviews, strategy = _extract(page.html, source, page.url)
                    added = result.add(reviews, strategy, limit)

                    # No new reviews means we are looping over the same page or
                    # have run past the end; either way, stop.
                    if not added:
                        result.notes.append(f"no new reviews on page {page_number + 1}; stopping")
                        break

                    page_url, html, page_number = page.url, page.html, page_number + 1

            # --- Pass 3: browser rendering --------------------------------
            # Only when HTTP found nothing: client-rendered review widgets are
            # invisible to a plain fetch.
            if not result.reviews and budget_left() > settings.playwright_timeout * 0.5:
                # A headless browser is still the same client asking for the
                # same page: if the site blocked us or robots.txt disallows it,
                # rendering it anyway would route around the rule.
                if result.blocked:
                    result.notes.append("skipping browser rendering: source blocked the scrape")
                elif not await fetcher.allowed(product_url):
                    result.notes.append("skipping browser rendering: disallowed by robots.txt")
                elif browser.available():
                    result.notes.append("no reviews over HTTP; trying browser rendering")
                    html = await browser.render(product_url)
                    if html:
                        reviews, strategy = _extract(html, source, product_url)
                        added = result.add(reviews, f"rendered:{strategy}", limit)
                        if added:
                            result.strategies.append("playwright")
                        result.pages_fetched += 1
                        logger.info("%s: %s review(s) after rendering", source, added)
                    else:
                        result.notes.append("browser rendering produced nothing")
                else:
                    result.notes.append(
                        "browser rendering unavailable (pip install playwright && playwright install chromium)"
                    )

    except Exception as error:
        # Belt and braces: the fetch layer already swallows its own failures,
        # so anything here is a bug rather than a site problem. Degrade instead
        # of turning a partial result into a 500.
        logger.exception(
            "host scrape failed",
            extra={"event_type": "scrape_error", "source": source, "role": "host"},
        )
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"

    if result.blocked and not result.reviews and not result.error:
        result.error = "host site blocked the scrape"
        logger.warning(
            "host scrape blocked",
            extra={"event_type": "scrape_blocked", "source": source, "role": "host"},
        )

    result.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "host scrape done source=%s count=%s pages=%s blocked=%s in %sms",
        source,
        len(result.reviews),
        result.pages_fetched,
        result.blocked,
        result.duration_ms,
    )
    return result
