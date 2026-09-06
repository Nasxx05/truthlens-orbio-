"""TikTok video discovery.

TikTok has no public search API, so this is scraping, and it is the least
reliable source in the system by a wide margin:

  * Search requires a rendered browser — the HTML served to a plain fetch
    carries no results.
  * Requests are signed in the web client; unsigned traffic is frequently
    challenged.
  * ``/search`` is disallowed by TikTok's robots.txt, so with
    ``RESPECT_ROBOTS=true`` (the default) this module reports that and returns
    nothing rather than routing around the rule.

It is therefore fully isolated: every failure path returns an empty
``VideoResult`` with an explanation, and the caller proceeds with whatever
other platforms produced. Nothing else in the request depends on it.

One deliberate difference from the YouTube module: the two-minute minimum is
*not* applied here. TikTok is a short-form platform where a useful review is
routinely 40 seconds, so the length filter that removes YouTube Shorts would
remove essentially every TikTok result. Relevance filtering still applies.
"""

import json
import logging
import re
import time
from typing import List, Optional
from urllib.parse import quote_plus

import httpx

from app.config import settings
from app.services.scrapers import browser
from app.services.scrapers.extract import soup_of
from app.services.scrapers.fetch import Fetcher, looks_blocked
from app.services.videos.base import Video, VideoResult
from app.services.videos.relevance import is_relevant

logger = logging.getLogger(__name__)

SOURCE = "tiktok"
SEARCH_URL = "https://www.tiktok.com/search?q={query}"
_VIDEO_HREF = re.compile(r"/@([\w.\-]+)/video/(\d{6,})")


def _videos_from_dom(html: str, product_title: str) -> List[Video]:
    """Extract results from rendered search markup."""
    soup = soup_of(html)
    found: List[Video] = []
    seen = set()

    for anchor in soup.select('a[href*="/video/"]'):
        href = anchor.get("href") or ""
        match = _VIDEO_HREF.search(href)
        if not match:
            continue
        author, video_id = match.group(1), match.group(2)
        if video_id in seen:
            continue

        # The caption is the closest thing TikTok has to a title, and it sits
        # in a sibling container rather than in the link. Walk up looking for
        # it, but stop as soon as the ancestor holds more than one video link:
        # past that point we have left this result and would attach the first
        # caption on the page to every video.
        caption = ""
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None or getattr(node, "name", None) in (None, "body", "html"):
                break
            if len(node.select('a[href*="/video/"]')) > 1:
                break
            caption_node = node.select_one(
                '[data-e2e="search-card-video-caption"], [class*="video-meta-caption" i], '
                '[data-e2e="search-card-desc"], [class*="caption" i]'
            )
            if caption_node:
                caption = " ".join(caption_node.get_text(" ", strip=True).split())
                if caption:
                    break

        if not caption:
            caption = " ".join(anchor.get_text(" ", strip=True).split())
        if not caption:
            caption = f"TikTok video by @{author}"

        seen.add(video_id)
        found.append(
            Video(
                title=caption[:300],
                url=f"https://www.tiktok.com/@{author}/video/{video_id}",
                source=SOURCE,
                channel=f"@{author}",
                channel_url=f"https://www.tiktok.com/@{author}",
                video_id=video_id,
            )
        )

    return found


def _videos_from_state(html: str) -> List[Video]:
    """Extract from the hydration JSON TikTok embeds in its pages.

    More reliable than the DOM when present, since class names change
    constantly while this payload is comparatively stable.
    """
    found: List[Video] = []

    for pattern in (
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
    ):
        match = re.search(pattern, html or "", re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        # The payload shape changes between releases, so walk it rather than
        # depending on a fixed path.
        def walk(node, depth=0):
            if depth > 10:
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)
                return
            if not isinstance(node, dict):
                return

            video_id = node.get("id") or node.get("awemeId")
            desc = node.get("desc")
            author = node.get("author")
            if video_id and isinstance(desc, str) and author:
                handle = author.get("uniqueId") if isinstance(author, dict) else author
                if handle and str(video_id).isdigit():
                    stats = node.get("stats") or {}
                    video_meta = node.get("video") or {}
                    found.append(
                        Video(
                            title=(desc or f"TikTok video by @{handle}")[:300],
                            url=f"https://www.tiktok.com/@{handle}/video/{video_id}",
                            source=SOURCE,
                            channel=f"@{handle}",
                            channel_url=f"https://www.tiktok.com/@{handle}",
                            duration_seconds=video_meta.get("duration") if isinstance(video_meta, dict) else None,
                            views=stats.get("playCount") if isinstance(stats, dict) else None,
                            video_id=str(video_id),
                        )
                    )
            for value in node.values():
                walk(value, depth + 1)

        walk(data)
        if found:
            break

    # Deduplicate, keeping first occurrence.
    unique, seen = [], set()
    for video in found:
        if video.video_id in seen:
            continue
        seen.add(video.video_id)
        unique.append(video)
    return unique


async def find_videos(
    product_title: str,
    *,
    limit: int = 4,
    html: Optional[str] = None,
) -> VideoResult:
    """Find TikTok videos about a product.

    ``html`` bypasses fetching and parses supplied markup instead, so the
    extraction and filtering path is testable without depending on TikTok
    being reachable or permitting the request.
    """
    started = time.monotonic()
    result = VideoResult(source=SOURCE)

    query = (product_title or "").strip()
    if not query:
        result.ok = False
        result.error = "no product title to search with"
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    if not settings.tiktok_enabled and html is None:
        result.notes.append("TikTok source disabled (TIKTOK_ENABLED=false)")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    url = SEARCH_URL.format(query=quote_plus(f"{query} review"))
    page_html = html

    try:
        if page_html is None:
            # robots.txt first: TikTok disallows /search, and a headless
            # browser is still us making the request.
            async with Fetcher() as fetcher:
                if not await fetcher.allowed(url):
                    result.blocked = True
                    result.notes.append(
                        "TikTok search is disallowed by its robots.txt; skipping. "
                        "Set RESPECT_ROBOTS=false to attempt it anyway."
                    )
                    result.duration_ms = int((time.monotonic() - started) * 1000)
                    return result

            if not browser.available():
                result.notes.append(
                    "TikTok search needs browser rendering "
                    "(pip install playwright && playwright install chromium)"
                )
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result

            page_html = await browser.render(url, click_more=1)
            if not page_html:
                result.ok = False
                result.error = "browser rendering returned nothing"
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result

            if looks_blocked(page_html, 200):
                result.blocked = True
                result.error = "TikTok served a challenge page instead of results"
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result

        # --- extract ---
        candidates = _videos_from_state(page_html) or _videos_from_dom(page_html, query)
        result.considered = len(candidates)

        if not candidates:
            result.notes.append("no video results found in the page")
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        # --- filter ---
        for video in candidates:
            keep, relevance, reason = is_relevant(
                query, video.title, settings.video_relevance_threshold
            )
            if not keep:
                result.filtered_out += 1
                logger.debug("tiktok dropping %r: %s", video.title[:60], reason)
                continue
            video.relevance = relevance
            result.videos.append(video)

        result.videos.sort(key=lambda v: (v.relevance or 0, v.views or 0), reverse=True)
        result.videos = result.videos[:limit]
        result.notes.append("length filter not applied: TikTok is short-form by design")

    except Exception as error:
        # Contained on purpose. This module is expected to be the flakiest in
        # the system and must never be able to affect the rest of a request.
        logger.warning(
            "video source failure",
            extra={"event_type": "video_source_error", "source": SOURCE,
                   "error_type": type(error).__name__},
        )
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"

    result.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "tiktok: %s kept, %s filtered, %s considered in %sms",
        len(result.videos), result.filtered_out, result.considered, result.duration_ms,
    )
    return result


__all__: List[str] = ["find_videos", "SOURCE"]
