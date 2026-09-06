"""YouTube video discovery via the official Data API v3.

Uses the documented API rather than scraping: YouTube's robots.txt disallows
``/results``, and the API returns better metadata anyway.

Two calls are required. ``search.list`` finds candidates but does not return
durations, so ``videos.list`` fetches ``contentDetails`` for the ids it found —
without which the "drop anything under two minutes" rule cannot be applied.

Requires ``YOUTUBE_API_KEY`` in the backend environment. Absent a key the
module reports that and returns nothing, which the caller treats as one source
contributing zero rather than as a failure.
"""

import logging
import os
import time
from typing import List, Optional

import httpx

from app.config import settings
from app.services.videos.base import Video, VideoResult
from app.services.videos.relevance import is_relevant, parse_iso8601_duration

logger = logging.getLogger(__name__)

SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

SOURCE = "youtube"


def _api_key() -> Optional[str]:
    """Read the key at call time, so the process need not restart to pick it up."""
    key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    return key or None


def _explain_http_error(response: httpx.Response) -> str:
    """Turn an API error into something actionable.

    Quota exhaustion and a bad key both arrive as 403, and the difference
    matters to whoever has to fix it.
    """
    try:
        payload = response.json()
        errors = payload.get("error", {})
        message = errors.get("message") or ""
        reasons = [d.get("reason", "") for d in errors.get("errors", []) if isinstance(d, dict)]
        reason = next((r for r in reasons if r), "")
    except Exception:
        message, reason = response.text[:200], ""

    if response.status_code == 403 and "quota" in (reason + message).lower():
        return "YouTube API quota exceeded for today"
    if response.status_code == 403:
        return f"YouTube API rejected the request ({reason or 'forbidden'}): {message[:120]}"
    if response.status_code == 400 and "api key not valid" in message.lower():
        return "YOUTUBE_API_KEY is not valid"
    return f"YouTube API HTTP {response.status_code}: {message[:120]}"


async def find_videos(
    product_title: str,
    *,
    limit: int = 6,
    min_seconds: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> VideoResult:
    """Find review videos for a product.

    ``client`` is injectable so the request/parse/filter path can be tested
    without live API access or a key.
    """
    started = time.monotonic()
    result = VideoResult(source=SOURCE)
    min_seconds = settings.video_min_seconds if min_seconds is None else min_seconds

    query = (product_title or "").strip()
    if not query:
        result.ok = False
        result.error = "no product title to search with"
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    key = _api_key()
    if not key and client is None:
        # Not an error: the source simply cannot contribute. The caller carries
        # on with whatever the other platforms returned.
        result.notes.append("YOUTUBE_API_KEY is not set; skipping YouTube")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=settings.video_timeout)

    try:
        # --- search.list ---
        search_params = {
            "part": "snippet",
            "q": f"{query} review",
            "type": "video",
            "maxResults": str(min(50, max(limit * 3, 10))),  # room to filter
            "relevanceLanguage": "en",
            "safeSearch": "none",
            "key": key or "test",
        }
        response = await client.get(SEARCH_ENDPOINT, params=search_params)
        if response.status_code != 200:
            result.ok = False
            result.error = _explain_http_error(response)
            result.blocked = response.status_code in (401, 403, 429)
            logger.warning(
                "video api failure",
                extra={"event_type": "video_api_error", "source": SOURCE,
                       "status": response.status_code, "error": result.error},
            )
            return result

        payload = response.json()
        items = payload.get("items") or []
        result.considered = len(items)

        candidates = {}
        for item in items:
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id or not snippet.get("title"):
                continue
            candidates[video_id] = snippet

        if not candidates:
            result.notes.append(f"no results for {query!r}")
            return result

        # --- videos.list, for durations and view counts ---
        details = {}
        try:
            detail_response = await client.get(
                VIDEOS_ENDPOINT,
                params={
                    "part": "contentDetails,statistics,snippet",
                    "id": ",".join(list(candidates)[:50]),
                    "key": key or "test",
                },
            )
            if detail_response.status_code == 200:
                for item in detail_response.json().get("items") or []:
                    details[item.get("id")] = item
            else:
                # Without durations the length filter cannot be applied. Say so
                # rather than silently returning shorts alongside reviews.
                result.notes.append(
                    f"could not fetch durations ({_explain_http_error(detail_response)}); "
                    "length filter not applied"
                )
        except httpx.HTTPError as error:
            result.notes.append(f"duration lookup failed: {type(error).__name__}; length filter not applied")

        # --- filter ---
        for video_id, snippet in candidates.items():
            title = snippet.get("title") or ""
            detail = details.get(video_id) or {}
            content = detail.get("contentDetails") or {}
            stats = detail.get("statistics") or {}

            seconds = parse_iso8601_duration(content.get("duration", ""))

            if seconds is not None and seconds < min_seconds:
                # Shorts and clips are not reviews.
                result.filtered_out += 1
                continue

            keep, relevance, reason = is_relevant(query, title, settings.video_relevance_threshold)
            if not keep:
                result.filtered_out += 1
                logger.debug("dropping %r: %s", title[:60], reason)
                continue

            views = stats.get("viewCount")
            channel_id = snippet.get("channelId")
            description = " ".join((snippet.get("description") or "").split())[:300] or None
            result.videos.append(
                Video(
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    source=SOURCE,
                    channel=snippet.get("channelTitle"),
                    channel_url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
                    duration_seconds=seconds,
                    published=(snippet.get("publishedAt") or "")[:10] or None,
                    views=int(views) if str(views).isdigit() else None,
                    thumbnail=((snippet.get("thumbnails") or {}).get("medium") or {}).get("url"),
                    relevance=relevance,
                    video_id=video_id,
                    description=description,
                )
            )

        # Most relevant first, then most watched: a well-viewed review is more
        # useful to a shopper than an equally relevant one nobody watched.
        result.videos.sort(key=lambda v: (v.relevance or 0, v.views or 0), reverse=True)
        result.videos = result.videos[:limit]

    except httpx.HTTPError as error:
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"
        logger.info("youtube request failed: %s", result.error)
    except Exception as error:
        logger.exception("youtube lookup failed")
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"
    finally:
        if owns_client and client is not None:
            await client.aclose()
        result.duration_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "youtube: %s kept, %s filtered, %s considered in %sms",
        len(result.videos), result.filtered_out, result.considered, result.duration_ms,
    )
    return result


__all__: List[str] = ["find_videos", "SOURCE"]
