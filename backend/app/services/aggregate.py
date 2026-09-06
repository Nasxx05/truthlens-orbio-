"""Concurrent collection across every data source.

All four sources — host reviews, competitor reviews, YouTube, TikTok — are
independent, so they run at once rather than in sequence. Two rules follow from
the architecture:

  * **One slow source cannot delay the others.** Each task carries its own
    timeout, and the gather collects whatever finished. A competitor site that
    hangs costs its own budget and nothing else.
  * **One broken source cannot fail the request.** Tasks are gathered with
    exceptions captured, and anything that raised is reported as a failed
    source alongside the results that succeeded.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from app.config import settings
from app.services.matching import MatchVerdict
from app.services.scrapers.base import Review, ScrapeResult
from app.services.scrapers.competitor import scrape_competitor_reviews
from app.services.scrapers.host import scrape_host_reviews
from app.services.videos import VideoResult
from app.services.videos import tiktok as tiktok_source
from app.services.videos import youtube as youtube_source

logger = logging.getLogger(__name__)


@dataclass
class Collection:
    """Everything gathered for one product."""

    host: ScrapeResult
    competitor: Optional[ScrapeResult] = None
    match: Optional[MatchVerdict] = None
    matched_product: Optional[dict] = None
    videos: List[VideoResult] = field(default_factory=list)
    duration_ms: Optional[int] = None

    @property
    def reviews(self) -> List[Review]:
        """Reviews from every source, each still tagged with its own platform.

        Host reviews come first, but ``source`` is what identifies them —
        divergence between platforms is the signal, so they are never pooled
        into an anonymous list.
        """
        merged = list(self.host.reviews)
        if self.competitor:
            merged.extend(self.competitor.reviews)
        return merged

    @property
    def review_sources(self) -> List[ScrapeResult]:
        return [r for r in (self.host, self.competitor) if r is not None]


_URL_NAME_SKIP_SEGMENTS = {
    "dp", "gp", "product", "products", "itm", "item", "site", "p", "ip", "pd",
    "s", "aspx", "html", "htm", "www",
}


def derive_name_from_url(url: Optional[str]) -> Optional[str]:
    """Best-effort product name guess from a URL path segment.

    Fallback only: when a host site blocks scraping outright (a 404/403 before
    any title is read), there is otherwise nothing to hand to competitor-site
    or video search — and a shopper on a blocked site would get no output at
    all. Most retail URLs embed the title in a slug (e.g.
    ``/Anker-PowerCore-10000/dp/B0XXXXX``), which is a decent search phrase
    even though it is not a scraped, verified name.
    """
    if not url:
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None

    best = ""
    for segment in path.split("/"):
        segment = unquote(segment).strip()
        if not segment or segment.lower() in _URL_NAME_SKIP_SEGMENTS:
            continue
        words = [w for w in re.split(r"[-_+]+", segment) if w and not w.isdigit()]
        # A bare product/model id (B08N5WRWNW, SKU123) has no separators to
        # split on and is not a usable search phrase.
        if len(words) < 2:
            continue
        candidate = " ".join(words)
        if len(candidate) > len(best):
            best = candidate

    return best or None


async def _guard(label: str, coro, timeout: float, fallback):
    """Run a source with its own timeout, never propagating a failure.

    Returns ``(value, note)``; ``value`` is ``fallback`` when the source timed
    out or raised, so the caller always has something to report.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout), None
    except asyncio.TimeoutError:
        logger.info("%s timed out after %.1fs", label, timeout)
        return fallback, f"{label} timed out after {timeout:.0f}s"
    except Exception as error:
        logger.exception("%s failed", label)
        return fallback, f"{label} failed: {type(error).__name__}: {error}"


async def collect(
    *,
    product_url: Optional[str],
    product_name: Optional[str],
    product_id: Optional[str] = None,
    site_hint: Optional[str] = None,
    want_competitor: bool = True,
    want_videos: bool = True,
) -> Collection:
    """Gather reviews and videos for one product, concurrently."""
    started = time.monotonic()

    host_task = asyncio.create_task(
        scrape_host_reviews(
            product_url=product_url or "",
            product_id=product_id,
            site_hint=site_hint,
        )
    )

    tasks = {"host": host_task}

    if want_competitor and (product_name or "").strip():
        tasks["competitor"] = asyncio.create_task(
            scrape_competitor_reviews(
                product_name,
                host_url=product_url,
                host_site=site_hint,
                product_meta={"product_id": product_id},
            )
        )

    if want_videos and (product_name or "").strip():
        tasks["youtube"] = asyncio.create_task(
            youtube_source.find_videos(product_name, limit=settings.video_max_results)
        )
        if settings.tiktok_enabled:
            tasks["tiktok"] = asyncio.create_task(
                tiktok_source.find_videos(product_name, limit=max(2, settings.video_max_results // 2))
            )

    # Every task is already running; awaiting them in turn does not serialize
    # them, and each carries its own timeout.
    host_result, host_note = await _guard(
        "host scrape", tasks["host"], settings.total_timeout + 5,
        ScrapeResult(source=site_hint or "host", ok=False, error="host scrape did not complete"),
    )
    if host_note:
        host_result.notes.append(host_note)

    collection = Collection(host=host_result)

    if "competitor" in tasks:
        fallback: Tuple[ScrapeResult, MatchVerdict, Optional[dict]] = (
            ScrapeResult(source="competitor", ok=False, error="competitor collection did not complete"),
            MatchVerdict(False, 0.0, "none", ["competitor collection did not complete"], []),
            None,
        )
        (competitor_result, verdict, matched), note = await _guard(
            "competitor scrape", tasks["competitor"], settings.competitor_timeout + 5, fallback
        )
        if note:
            competitor_result.notes.append(note)
        collection.competitor = competitor_result
        collection.match = verdict
        collection.matched_product = matched

    for name in ("youtube", "tiktok"):
        if name not in tasks:
            continue
        module = youtube_source if name == "youtube" else tiktok_source
        result, note = await _guard(
            f"{name} lookup", tasks[name], settings.video_timeout + 10,
            VideoResult(source=module.SOURCE, ok=False, error=f"{name} lookup did not complete"),
        )
        if note:
            result.notes.append(note)
        collection.videos.append(result)

    collection.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "collected host=%s competitor=%s videos=%s in %sms",
        len(collection.host.reviews),
        len(collection.competitor.reviews) if collection.competitor else "-",
        sum(len(v.videos) for v in collection.videos),
        collection.duration_ms,
    )
    return collection


async def collect_streaming(
    *,
    product_url: Optional[str],
    product_name: Optional[str],
    product_id: Optional[str] = None,
    site_hint: Optional[str] = None,
    want_competitor: bool = True,
    want_videos: bool = True,
):
    """Gather sources concurrently, yielding each as it finishes.

    Same work as :func:`collect`, but as an async generator so a caller can
    forward partial results instead of waiting for the slowest source. Yields
    ``(kind, payload)`` where kind is ``"host"``, ``"competitor"``, ``"video"``
    or ``"done"``; the final ``"done"`` payload is the assembled
    :class:`Collection`.

    Ordering is arrival order, not a fixed sequence — that is the point. The
    host site usually lands first, videos whenever their platform answers.
    """
    started = time.monotonic()

    tasks = {}
    tasks["host"] = asyncio.create_task(
        scrape_host_reviews(
            product_url=product_url or "",
            product_id=product_id,
            site_hint=site_hint,
        )
    )
    if want_competitor and (product_name or "").strip():
        tasks["competitor"] = asyncio.create_task(
            scrape_competitor_reviews(
                product_name,
                host_url=product_url,
                host_site=site_hint,
                product_meta={"product_id": product_id},
            )
        )
    if want_videos and (product_name or "").strip():
        tasks["youtube"] = asyncio.create_task(
            youtube_source.find_videos(product_name, limit=settings.video_max_results)
        )
        if settings.tiktok_enabled:
            tasks["tiktok"] = asyncio.create_task(
                tiktok_source.find_videos(product_name, limit=max(2, settings.video_max_results // 2))
            )

    timeouts = {
        "host": settings.total_timeout + 5,
        "competitor": settings.competitor_timeout + 5,
        "youtube": settings.video_timeout + 10,
        "tiktok": settings.video_timeout + 10,
    }

    collection = Collection(host=ScrapeResult(source=site_hint or "host", ok=False))

    async def guarded(name: str, task, timeout: float):
        """Await one task, mapping timeout/failure to a reportable value."""
        try:
            return name, await asyncio.wait_for(task, timeout=timeout), None
        except asyncio.TimeoutError:
            return name, None, f"{name} timed out after {timeout:.0f}s"
        except Exception as error:
            logger.exception("%s failed", name)
            return name, None, f"{name} failed: {type(error).__name__}: {error}"

    pending = [
        asyncio.create_task(guarded(name, task, timeouts.get(name, 30.0)))
        for name, task in tasks.items()
    ]

    while pending:
        finished, pending_set = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        pending = list(pending_set)

        for completed in finished:
            name, value, note = completed.result()

            if name == "host":
                result = value or ScrapeResult(
                    source=site_hint or "host", ok=False, error="host scrape did not complete"
                )
                if note:
                    result.notes.append(note)
                collection.host = result
                yield "host", result

            elif name == "competitor":
                if value is None:
                    result = ScrapeResult(
                        source="competitor", ok=False, error="competitor collection did not complete"
                    )
                    verdict = MatchVerdict(False, 0.0, "none", ["did not complete"], [])
                    matched = None
                else:
                    result, verdict, matched = value
                if note:
                    result.notes.append(note)
                collection.competitor = result
                collection.match = verdict
                collection.matched_product = matched
                # Verdict travels with the result: the caller needs it to emit
                # the match event without waiting for the whole collection.
                yield "competitor", (result, verdict, matched)

            else:
                module = youtube_source if name == "youtube" else tiktok_source
                result = value or VideoResult(
                    source=module.SOURCE, ok=False, error=f"{name} lookup did not complete"
                )
                if note:
                    result.notes.append(note)
                collection.videos.append(result)
                yield "video", result

    collection.duration_ms = int((time.monotonic() - started) * 1000)
    yield "done", collection


def merged_videos(results: List[VideoResult]) -> List[dict]:
    """Videos from every platform, interleaved and tagged with their source.

    Interleaved rather than concatenated so one platform cannot monopolize the
    top of the list — a shopper should see both a YouTube review and a TikTok
    take without scrolling.
    """
    ranked = [sorted(r.videos, key=lambda v: v.relevance or 0, reverse=True) for r in results if r.videos]
    merged: List[dict] = []
    index = 0
    while any(index < len(group) for group in ranked):
        for group in ranked:
            if index < len(group):
                merged.append(group[index].to_dict())
        index += 1
    return merged
