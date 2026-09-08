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
from app.services.pipeline import INVESTIGATION_STAGES, _stage, analyze_once, analyze_stream, sse

logger = logging.getLogger(__name__)

router = APIRouter()

# Safe, user-facing copy for every way a request can be rejected or fail.
# Never includes the raw internal reason — that goes to the log line above
# each raise, for operators, not shoppers. Codes let the frontend pick exact
# wording without string-matching a message.
ERROR_COPY = {
    "invalid_url": (
        "That doesn't appear to be a valid product URL. Please enter a product page URL."
    ),
    "unsupported_target": (
        "TrustLens can't analyze that link — it points to a network address this "
        "service isn't allowed to fetch. Please use a public product page URL."
    ),
    "no_product": (
        "We couldn't confidently identify a product from this page. Try pasting the "
        "product's direct URL, or type its name instead."
    ),
    "rate_limited": "TrustLens is receiving a lot of requests right now. Please wait a moment and try again.",
    "server_error": "TrustLens couldn't complete the investigation. Please try again.",
}

# Substrings from `validate_target`'s reason, classified into the two shopper-
# facing buckets: a malformed URL (fix what you typed) vs. a URL this service
# will never fetch regardless of syntax (SSRF/private-network guard).
_INVALID_URL_MARKERS = (
    "could not be parsed", "scheme", "no host",
)


def _classify_url_rejection(reason: Optional[str]) -> str:
    reason = (reason or "").lower()
    if any(marker in reason for marker in _INVALID_URL_MARKERS):
        return "invalid_url"
    return "unsupported_target"


async def _prepare(payload: AnalyzeRequest) -> Tuple[Optional[str], Optional[str]]:
    """Validate the target and derive its cache key.

    Raises ``HTTPException`` for a URL this service must not fetch.
    """
    target = payload.canonical_url or payload.product_url
    detection = payload.detection

    verdict = await validate_target(target)
    if not verdict.allowed:
        code = _classify_url_rejection(verdict.reason)
        logger.warning(
            "rejected analysis target",
            extra={"event_type": "target_rejected", "reason": verdict.reason, "code": code},
        )
        # The precise reason stays server-side (log line above); the client
        # only ever sees the safe canned copy for its bucket.
        raise HTTPException(status_code=400, detail={"code": code, "message": ERROR_COPY[code]})

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


def _cached_stage_events(payload: dict) -> list:
    """Investigation-trail burst for a cache replay.

    Every real check already ran when this was first analyzed; this reads
    the same *stored* result the content sections render from — never a new
    computation — to tell the rail what actually happened, all at once,
    rather than leaving it on "pending" forever for a cached response.
    """
    summary = payload.get("summary") or {}
    reviews_passed = payload.get("reviews_passed", 0)
    reviews = payload.get("reviews") or []
    contributed = set(payload.get("contributed") or [])
    sources = payload.get("sources") or []
    host = next((s for s in sources if s.get("source") not in (None, "competitor")), None) or {}
    attempted_competitor = any(s.get("source") == "competitor" for s in sources) or any(
        "competitor" in c for c in contributed
    )
    videos_present = bool(payload.get("videos"))

    events = [_stage("product_identification", "complete", detail="from cached analysis")]
    events.append(
        _stage(
            "product_info_collected", "complete",
            detail="Image and/or description found" if (host.get("image_url") or host.get("description"))
            else "No image or description found",
        )
    )
    events.append(
        _stage(
            "reviews_collected", "complete" if reviews_passed else "failed",
            detail=f"{reviews_passed} of {len(reviews)} review(s) passed filtering",
        )
    )
    events.append(_stage("sentiment_analyzed", "complete" if reviews else "skipped"))
    events.append(_stage("pattern_analysis", "complete" if reviews else "skipped"))
    events.append(
        _stage("review_reliability", "complete" if summary.get("review_risk") else "skipped")
    )
    if not attempted_competitor and not videos_present:
        events.append(_stage("external_research", "skipped", detail="No product name to cross-check with"))
    else:
        events.append(_stage("external_research", "complete" if videos_present or attempted_competitor else "failed"))
    events.append(
        _stage("claim_research", "complete" if summary.get("claim_check") else "skipped")
    )
    has_verdict = bool(summary.get("verdict"))
    events.append(_stage("evidence_synthesis", "complete" if has_verdict else "failed"))
    events.append(_stage("trust_score", "complete" if has_verdict else "failed"))
    events.append(_stage("verdict", "complete" if has_verdict else "failed"))
    return events


def _cached_events(payload: dict, info: CacheInfo):
    """Replay a cached result as the event sequence a live run would produce.

    The popup has one rendering path; a cached result taking a different shape
    would mean a second one, which would rot.
    """
    yield {
        "event": "started",
        "stages": ["reviews", "videos", "summary"],
        "investigation": [{"id": sid, "label": label} for sid, label in INVESTIGATION_STAGES],
        "cached": True,
    }
    for stage_event in _cached_stage_events(payload):
        yield stage_event
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
        "partial": payload.get("partial", False),
        "partial_reasons": payload.get("partial_reasons", []),
    }


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(payload: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    """Analyze a product page and return the complete result.

    Every branch below is wrapped so a caller never sees a raw exception:
    a rejected target is a clean 400 with canned copy (see ``_prepare``), a
    worker/pipeline failure is a clean 502/500 with canned copy, and
    anything truly unexpected still falls through to ``main.py``'s outer
    middleware catch — this is belt-and-suspenders, not the only guard.
    """
    target, key = await _prepare(payload)

    try:
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
            assembled: dict = {"partial": False, "partial_reasons": []}
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
                        "partial": event.get("partial", False),
                        "partial_reasons": event.get("partial_reasons", []),
                    })
                elif name == "error":
                    logger.warning(
                        "worker reported analysis failure",
                        extra={"event_type": "job_error", "detail": event.get("message")},
                    )
                    raise HTTPException(
                        status_code=502,
                        detail={"code": "server_error", "message": ERROR_COPY["server_error"]},
                    )

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
    except HTTPException:
        raise
    except Exception:
        # analyze_once/pump_summary already guard themselves; this is the
        # last line of defense for anything that still escapes (a queue
        # client error, a schema mismatch assembling `assembled`, etc).
        logger.exception("analyze failed unexpectedly", extra={"event_type": "analyze_error"})
        raise HTTPException(
            status_code=500,
            detail={"code": "server_error", "message": ERROR_COPY["server_error"]},
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
                        "partial": event.get("partial", False),
                        "partial_reasons": event.get("partial_reasons", []),
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
        except Exception:
            # The 200 has already been sent, so this cannot become an HTTP
            # error; report it in-band instead of truncating the stream. The
            # real exception is logged server-side only — the client gets
            # the same safe canned copy every other failure path uses.
            logger.exception("streaming analysis failed", extra={"event_type": "stream_error"})
            yield sse({
                "event": "error",
                "code": "server_error",
                "message": ERROR_COPY["server_error"],
            })

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
