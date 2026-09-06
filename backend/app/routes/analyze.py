"""The analyze routes.

Two endpoints over one pipeline:

``POST /analyze``         the complete result.
``POST /analyze/stream``  each stage as it finishes, as Server-Sent Events.

Before any work happens, a request goes through three gates:

1. **Target validation.** ``product_url`` is fetched by this backend, so it is
   checked against the SSRF rules first. A rejected URL never reaches a
   scraper, a queue, or the cache.
2. **Cache.** A hit returns immediately and costs no scraping and no LLM call.
   The streaming endpoint replays a cached result as the same event sequence a
   live analysis produces, so the popup renders identically either way.
3. **Queue.** A miss is handed to a worker, and the endpoint follows the job's
   event stream. With no Redis the analysis runs inline instead, which is
   correct but does not shed load.
"""

import logging
from typing import Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.db import cache, cache_key
from app.models.schemas import AnalyzeRequest, AnalyzeResponse, CacheInfo
from app.queue import Job, enqueue, follow, new_job_id
from app.security import validate_target
from app.services.pipeline import analyze_once, analyze_stream, sse

logger = logging.getLogger(__name__)

router = APIRouter()


async def _prepare(payload: AnalyzeRequest) -> Tuple[Optional[str], Optional[str]]:
    """Validate the target and derive its cache key.

    Raises ``HTTPException`` for a URL this service must not fetch.
    """
    target = payload.canonical_url or payload.product_url
    detection = payload.detection

    verdict = await validate_target(target)
    if not verdict.allowed:
        logger.warning(
            "rejected analysis target",
            extra={"event_type": "target_rejected", "reason": verdict.reason},
        )
        # The reason is returned deliberately: it tells a legitimate caller
        # what to fix, and tells an attacker only that the check exists.
        raise HTTPException(status_code=400, detail=f"URL not allowed: {verdict.reason}")

    key = cache_key(
        product_id=payload.product_id,
        site=detection.site if detection else None,
        canonical_url=target,
        product_name=payload.product_name,
    )

    logger.info(
        "analyze requested",
        extra={
            "event_type": "analyze_request",
            "product_id": payload.product_id,
            "site": detection.site if detection else None,
            "detection_source": detection.source if detection else None,
            "cache_key": key[:8] if key else None,
            "refresh": payload.refresh,
        },
    )
    return target, key


def _job_for(payload: AnalyzeRequest, target: Optional[str], key: Optional[str]) -> Job:
    detection = payload.detection
    return Job(
        job_id=new_job_id(),
        product_url=target,
        product_name=payload.product_name,
        product_id=payload.product_id,
        site_hint=detection.site if detection else None,
        cache_key=key,
    )


def _cached_events(payload: dict, info: CacheInfo):
    """Replay a cached result as the event sequence a live run would produce.

    The popup has one rendering path; a cached result taking a different shape
    would mean a second one, which would rot.
    """
    yield {"event": "started", "stages": ["reviews", "videos", "summary"], "cached": True}
    yield {
        "event": "reviews",
        "reviews": payload.get("reviews", []),
        "reviews_passed": payload.get("reviews_passed", 0),
        "filter_report": payload.get("filter_report"),
        "sources": payload.get("sources", []),
    }
    if payload.get("product_match"):
        yield {"event": "match", "product_match": payload["product_match"]}
    videos = payload.get("videos", [])
    if videos:
        yield {"event": "videos", "source": "cache", "report": {}, "videos": videos}
    yield {
        "event": "summary",
        "summary": payload.get("summary", {}),
        "llm": payload.get("llm") or {},
    }
    yield {
        "event": "done",
        "status": payload.get("status", "ok"),
        "message": payload.get("message"),
        "contributed": payload.get("contributed", []),
        "video_sources": payload.get("video_sources", []),
        "videos": videos,
        "duration_ms": 0,
        "cached": info.model_dump(),
    }


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(payload: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    """Analyze a product page and return the complete result."""
    target, key = await _prepare(payload)

    # --- cache ---
    if key and not payload.refresh:
        hit = await cache.get(key)
        if hit is not None:
            logger.info(
                "cache hit",
                extra={"event_type": "cache_hit", "cache_key": key[:8], "age_s": hit.age_seconds},
            )
            result = dict(hit.payload)
            result["cached"] = CacheInfo(
                hit=True, age_seconds=hit.age_seconds, expires_at=hit.expires_at, key=key
            ).model_dump()
            return AnalyzeResponse(**result)

    # --- queue ---
    job = _job_for(payload, target, key)
    followed = await enqueue(job)

    if followed:
        assembled: dict = {}
        async for event in follow(followed):
            name = event.get("event")
            if name == "reviews":
                assembled.update({
                    "reviews": event["reviews"],
                    "reviews_passed": event["reviews_passed"],
                    "filter_report": event["filter_report"],
                    "sources": event["sources"],
                })
            elif name == "summary":
                assembled.update({"summary": event["summary"], "llm": event["llm"]})
            elif name == "match":
                assembled["product_match"] = event["product_match"]
            elif name == "done":
                assembled.update({
                    "status": event["status"], "message": event["message"],
                    "contributed": event["contributed"],
                    "video_sources": event["video_sources"], "videos": event["videos"],
                })
            elif name == "error":
                raise HTTPException(status_code=502, detail=event.get("message", "analysis failed"))

        assembled["cached"] = CacheInfo(hit=False, key=key).model_dump()
        return AnalyzeResponse(**assembled)

    # --- inline (no queue available) ---
    detection = payload.detection
    result = await analyze_once(
        product_url=target,
        product_name=payload.product_name,
        product_id=payload.product_id,
        site_hint=detection.site if detection else None,
    )
    if key:
        await cache.put(
            key, result,
            status=result.get("status", "ok"),
            product_id=payload.product_id,
            site=detection.site if detection else None,
            product_name=payload.product_name,
            canonical_url=target,
            review_count=result.get("reviews_passed", 0),
        )
    result["cached"] = CacheInfo(hit=False, key=key).model_dump()
    return AnalyzeResponse(**result)


@router.post("/analyze/stream")
async def analyze_streaming(payload: AnalyzeRequest, request: Request) -> StreamingResponse:
    """Analyze a product page, streaming each stage as it completes."""
    target, key = await _prepare(payload)

    cached = None
    if key and not payload.refresh:
        cached = await cache.get(key)

    async def events():
        try:
            if cached is not None:
                logger.info(
                    "cache hit (stream)",
                    extra={"event_type": "cache_hit", "cache_key": key[:8],
                           "age_s": cached.age_seconds},
                )
                info = CacheInfo(
                    hit=True, age_seconds=cached.age_seconds,
                    expires_at=cached.expires_at, key=key,
                )
                for event in _cached_events(cached.payload, info):
                    yield sse(event)
                return

            job = _job_for(payload, target, key)
            followed = await enqueue(job)

            if followed:
                async for event in follow(followed):
                    yield sse(event)
                return

            # Inline fallback: no queue, so this process does the work and
            # caches the outcome itself.
            detection = payload.detection
            assembled: dict = {}
            async for event in analyze_stream(
                product_url=target,
                product_name=payload.product_name,
                product_id=payload.product_id,
                site_hint=detection.site if detection else None,
            ):
                name = event.get("event")
                if name == "reviews":
                    assembled.update({
                        "reviews": event["reviews"], "reviews_passed": event["reviews_passed"],
                        "filter_report": event["filter_report"], "sources": event["sources"],
                    })
                elif name == "summary":
                    assembled.update({"summary": event["summary"], "llm": event["llm"]})
                elif name == "match":
                    assembled["product_match"] = event["product_match"]
                elif name == "done":
                    assembled.update({
                        "status": event["status"], "message": event["message"],
                        "contributed": event["contributed"],
                        "video_sources": event["video_sources"], "videos": event["videos"],
                    })
                yield sse(event)

            if key and assembled.get("status"):
                await cache.put(
                    key, assembled,
                    status=assembled["status"],
                    product_id=payload.product_id,
                    site=detection.site if detection else None,
                    product_name=payload.product_name,
                    canonical_url=target,
                    review_count=assembled.get("reviews_passed", 0),
                )
        except Exception as error:
            # The 200 has already been sent, so this cannot become an HTTP
            # error; report it in-band instead of truncating the stream.
            logger.exception("streaming analysis failed", extra={"event_type": "stream_error"})
            yield sse({"event": "error", "message": f"{type(error).__name__}: {error}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cache/invalidate")
async def invalidate(payload: AnalyzeRequest) -> dict:
    """Drop a product's cached analysis, so the next lookup re-runs it."""
    key = cache_key(
        product_id=payload.product_id,
        site=payload.detection.site if payload.detection else None,
        canonical_url=payload.canonical_url or payload.product_url,
        product_name=payload.product_name,
    )
    if not key:
        raise HTTPException(status_code=400, detail="could not identify a product to invalidate")
    dropped = await cache.invalidate(key)
    return {"invalidated": dropped, "key": key}
