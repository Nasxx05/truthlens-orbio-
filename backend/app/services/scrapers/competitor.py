"""Competitor-site review collection.

The competitor site is a *different* platform carrying the *same* product. Its
reviews exist to be compared against the host site's, so two things matter more
here than in host scraping:

  * **The product must actually be the same one.** A near-miss — the previous
    generation, a different capacity, an accessory — produces reviews that read
    as evidence about this product and are not. So a candidate is scored by
    ``app.services.matching`` and the source is *omitted* unless the match
    clears the confidence threshold. Omitting is the correct failure mode.
  * **Reviews stay attributed.** They are tagged with their own platform and
    never pooled with the host's, because divergence between platforms is
    itself the signal worth surfacing.
"""

import asyncio
import logging
import time
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from app.config import settings
from app.services.matching import MatchVerdict, best_match
from app.services.scrapers.base import ScrapeResult
from app.services.scrapers.fetch import Fetcher
from app.services.scrapers.host import _extract, _source_name
from app.services.scrapers.sites import search_adapter

logger = logging.getLogger(__name__)


def _host_domain(url: str, site_hint: Optional[str]) -> str:
    """Normalized host identity, used to avoid comparing a site with itself."""
    if site_hint:
        return site_hint.lower()
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _is_same_site(candidate_name: str, candidate_domain: str, host: str) -> bool:
    """Would this competitor be the host site under another name?"""
    if not host:
        return False
    return candidate_name in host or candidate_domain in host or host in candidate_domain


def search_query(product_name: Optional[str], product_id: Optional[str]) -> Optional[str]:
    """What to type into the competitor's search box.

    The product title is the useful query. A site-specific id (an ASIN, say) is
    meaningless on another site, so it is never used as the query — searching a
    competitor for "B09XS7JWHH" returns nothing at best and something unrelated
    at worst.
    """
    name = (product_name or "").strip()
    if not name:
        return None
    # Long marketing titles hurt on-site search; the first ten tokens carry the
    # brand and model, which is what matching needs.
    return " ".join(name.split()[:10])


async def scrape_competitor_reviews(
    product_name: Optional[str],
    *,
    host_url: Optional[str] = None,
    host_site: Optional[str] = None,
    product_meta: Optional[dict] = None,
    limit: Optional[int] = None,
) -> Tuple[ScrapeResult, MatchVerdict, Optional[dict]]:
    """Find the same product on a competitor site and scrape its reviews.

    Returns ``(result, verdict, matched_product)``. When nothing matched
    confidently, ``result`` carries zero reviews and the verdict explains why —
    the caller reports the source as attempted rather than silently absent.
    """
    started = time.monotonic()
    limit = limit or settings.competitor_review_max
    result = ScrapeResult(source="competitor")
    verdict = MatchVerdict(False, 0.0, "none", ["competitor search not attempted"], [])

    query = search_query(product_name, (product_meta or {}).get("product_id"))
    if not query:
        result.ok = False
        result.error = "no product name to search a competitor with"
        result.notes.append(
            "Competitor lookup needs a product title; an id from the host site "
            "does not identify the product elsewhere."
        )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result, verdict, None

    host = _host_domain(host_url or "", host_site)
    attempted: List[str] = []
    matched_product: Optional[dict] = None

    def remaining() -> float:
        return settings.competitor_timeout - (time.monotonic() - started)

    async def fetch_within_budget(fetcher, url):
        """Fetch, but never for longer than the competitor budget allows.

        Checking the budget between requests is not enough: a single stalled
        request would still run to its own timeout and overshoot. Returns
        ``None`` when the budget ran out mid-request.
        """
        budget = remaining()
        if budget <= 1:
            return None
        try:
            return await asyncio.wait_for(fetcher.get(url), timeout=budget)
        except asyncio.TimeoutError:
            return None

    try:
        async with Fetcher() as fetcher:
            for name in settings.competitor_sites:
                adapter = search_adapter(name)
                if adapter is None:
                    result.notes.append(f"unknown competitor site '{name}'")
                    continue

                # Never compare a site against itself: the "competitor" reviews
                # would be the same reviews, and any divergence signal is lost.
                if _is_same_site(adapter.name, adapter.domain, host):
                    result.notes.append(f"skipping {adapter.name}: shopper is already on it")
                    continue

                attempted.append(adapter.name)
                if remaining() <= 3:
                    result.notes.append("competitor budget exhausted")
                    break

                # --- search ---
                search_url = adapter.search_url(query)
                page = await fetch_within_budget(fetcher, search_url)
                result.pages_fetched += 1

                if page is None:
                    result.notes.append(
                        f"{adapter.name} search exceeded the "
                        f"{settings.competitor_timeout:.0f}s competitor budget"
                    )
                    break

                if page.blocked:
                    result.blocked = True
                    result.notes.append(f"{adapter.name} search blocked: {page.error}")
                    continue
                if not page.ok or not page.html:
                    result.notes.append(f"{adapter.name} search failed: {page.error or 'no content'}")
                    continue

                try:
                    candidates = adapter.parse_search(page.html, page.url) or []
                except Exception as error:
                    logger.debug("%s search parse failed: %s", adapter.name, error)
                    candidates = []

                if not candidates:
                    result.notes.append(f"{adapter.name}: search returned no candidate products")
                    continue

                result.notes.append(f"{adapter.name}: {len(candidates)} candidate(s) for {query!r}")

                # --- match ---
                matched, verdict = best_match(
                    product_name or "",
                    candidates,
                    query_meta=product_meta,
                    threshold=settings.match_threshold,
                )

                if not matched:
                    # Deliberately not a fallback to "closest thing": reviews of
                    # a different product would be indistinguishable from real
                    # evidence once merged.
                    reason = verdict.conflicts[0] if verdict.conflicts else (
                        f"best candidate scored {verdict.score:.2f}, below threshold "
                        f"{settings.match_threshold:.2f}"
                    )
                    result.notes.append(f"{adapter.name}: no confident match — {reason}")
                    continue

                matched_product = matched
                logger.info(
                    "competitor match on %s: %r (score %.3f, %s)",
                    adapter.name, matched.get("title"), verdict.score, verdict.confidence,
                )
                result.source = _source_name(matched.get("url", ""), adapter.name)
                result.notes.append(
                    f"matched {matched.get('title')!r} on {adapter.name} "
                    f"(score {verdict.score:.2f}, {verdict.confidence})"
                )

                # --- reviews ---
                for review_url in adapter.review_urls(matched.get("url", ""), None)[:2]:
                    if len(result.reviews) >= limit:
                        break
                    if remaining() <= 2:
                        result.notes.append("competitor budget exhausted before reviews")
                        break

                    review_page = await fetch_within_budget(fetcher, review_url)
                    result.pages_fetched += 1

                    if review_page is None:
                        result.notes.append("competitor budget exhausted while fetching reviews")
                        break

                    if review_page.blocked:
                        result.blocked = True
                        result.notes.append(f"{adapter.name} reviews blocked: {review_page.error}")
                        continue
                    if not review_page.ok or not review_page.html:
                        continue

                    reviews, strategy = _extract(review_page.html, result.source, review_page.url)
                    added = result.add(reviews, strategy, limit)
                    logger.info("competitor %s: %s review(s) via %s", result.source, added, strategy)

                    if added:
                        # Paginate from whichever URL produced reviews.
                        page_url, html, page_number = review_page.url, review_page.html, 1
                        while (
                            page_number < settings.max_pages
                            and len(result.reviews) < limit
                            and remaining() > 3
                        ):
                            try:
                                next_url = adapter.next_page(html, page_url, page_number)
                            except Exception:
                                break
                            if not next_url or next_url == page_url:
                                break
                            nxt = await fetch_within_budget(fetcher, next_url)
                            result.pages_fetched += 1
                            if nxt is None or not nxt.ok or not nxt.html or nxt.blocked:
                                break
                            more, strategy = _extract(nxt.html, result.source, nxt.url)
                            if not result.add(more, strategy, limit):
                                break
                            page_url, html, page_number = nxt.url, nxt.html, page_number + 1

                if result.reviews:
                    break  # one confident competitor is the goal, not all of them

    except Exception as error:
        logger.exception(
            "competitor scrape failed",
            extra={"event_type": "scrape_error", "role": "competitor"},
        )
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"

    if attempted and not result.notes:
        result.notes.append(f"attempted: {', '.join(attempted)}")

    if not result.reviews and not result.error:
        if result.blocked:
            result.error = "competitor site(s) blocked the scrape"
        elif not verdict.matched:
            result.error = "no confident product match on any competitor site"

    result.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "competitor done source=%s count=%s matched=%s score=%.2f in %sms",
        result.source, len(result.reviews), verdict.matched, verdict.score, result.duration_ms,
    )
    return result, verdict, (matched_product if verdict.matched else None)
