"""The analysis pipeline, as a stream of events.

Both endpoints run this: ``/analyze/stream`` forwards the events as they are
produced, and ``/analyze`` consumes the same generator to assemble one
response. That is deliberate — two implementations of "collect, filter,
summarize" would drift, and the streaming one would quietly become the
untested path.

Event order reflects when work actually finishes, not a fixed script. Reviews
are never gated on videos, and the summary comes last because it depends on
filtering, which depends on every review source. Under normal conditions a
shopper sees reviews long before the verdict.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Dict, List, Optional

from app.config import settings
from app.services.aggregate import collect_streaming, derive_name_from_url, merged_videos
from app.services.currency import usd_amount
from app.services.nlp import filter_reviews
from app.services.llm import SummaryResult
from app.services.summarize import SummaryBundle, summarize_reviews

logger = logging.getLogger(__name__)


# The investigation trail shown in the UI, in order. This is the single
# source of truth for stage ids/labels — the frontend renders whatever the
# backend sends in the `started` event's `investigation` list rather than
# keeping its own hardcoded copy, so the rail can never drift from what the
# backend actually does. Every id below is backed by a real computation
# already happening elsewhere in this module or in `summarize.py`/`risk.py` —
# none of these represent work that doesn't otherwise occur.
INVESTIGATION_STAGES = [
    ("product_identification", "Product identified"),
    ("product_info_collected", "Product information collected"),
    ("reviews_collected", "Reviews collected"),
    ("sentiment_analyzed", "Customer sentiment analyzed"),
    ("pattern_analysis", "Checking recurring patterns"),
    ("review_reliability", "Checking review reliability"),
    ("external_research", "Searching external sources"),
    ("claim_research", "Cross-checking product claims"),
    ("evidence_synthesis", "Synthesizing evidence"),
    ("trust_score", "Calculating Trust Score"),
    ("verdict", "Generating verdict"),
]


def _stage(stage_id: str, status: str, detail: Optional[str] = None) -> dict:
    """One investigation-trail update.

    ``status`` is one of ``running | complete | failed | skipped``. Every
    call site passes a ``detail`` derived from real data already collected —
    never a placeholder — so the UI can show *why* a step failed or was
    skipped instead of just a red mark.
    """
    label = dict(INVESTIGATION_STAGES).get(stage_id, stage_id)
    return {"event": "stage", "id": stage_id, "label": label, "status": status, "detail": detail}


def _explain(host_report: Optional[dict], total: int, minimum: int, filtered_out: int) -> str:
    """Say why the review set is too thin to work with."""
    if total > 0 and filtered_out and total < minimum:
        return (
            f"Only {total} of {total + filtered_out} reviews passed fake-review filtering; "
            f"{minimum} are needed. See filter_report for which rules fired."
        )
    host = host_report or {}
    source = host.get("source") or "the host site"
    if host.get("blocked"):
        return (
            f"{source} blocked the request, so its reviews could not be read. "
            "This is common on large retailers that detect automated access."
        )
    if not host.get("ok") and host.get("error"):
        return f"Could not read reviews from {source}: {host['error']}."
    if total == 0:
        return (
            "No reviews were found for this product. It may have none, or they may be "
            "loaded in a way this scraper cannot read yet."
        )
    return (
        f"Only {total} review(s) found across all sources; "
        f"{minimum} are needed to say anything useful about this product."
    )


async def analyze_stream(
    *,
    product_url: Optional[str],
    product_name: Optional[str],
    product_id: Optional[str] = None,
    site_hint: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Run the pipeline, yielding events in the order work actually finishes.

    Events:

    ``started``   the pipeline is running; lists the stages to expect
    ``source``    one review source finished (report only — reviews follow filtering)
    ``videos``    one video platform finished, with its videos
    ``reviews``   every review, annotated with its filtering verdict
    ``summary``   the LLM verdict
    ``done``      final status and totals
    ``error``     the pipeline itself failed

    Stages are genuinely independent. Reviews are emitted as soon as the review
    sources report — never waiting on a video platform — and summarization
    starts at that moment, overlapping the remaining video lookups. Everything
    is funnelled through one queue so each event is forwarded when it is ready
    rather than in a fixed sequence.
    """
    started = time.monotonic()

    # A caller that only has a URL (the common case for a pasted link) sends
    # no product_name — but a host site that blocks the scrape outright leaves
    # nothing to search video platforms with. Falling back to a name guessed
    # from the URL slug means a blocked host still produces output, instead
    # of "not enough data" with nothing tried.
    if not (product_name or "").strip():
        product_name = derive_name_from_url(product_url)

    yield {
        "event": "started",
        "stages": ["reviews", "videos", "summary"],
        "investigation": [{"id": sid, "label": label} for sid, label in INVESTIGATION_STAGES],
        "product": {"name": product_name, "url": product_url, "id": product_id},
    }

    # Identification happens synchronously above (from the URL/name the
    # caller supplied), so it is already complete by the time anything else
    # can run. The next few stages begin the instant source collection
    # starts, below.
    yield _stage(
        "product_identification", "complete",
        detail=product_name or product_url or "identified from request",
    )
    yield _stage("product_info_collected", "running")
    yield _stage("reviews_collected", "running")
    yield _stage("external_research", "running")

    review_sources: List[dict] = []
    video_sources: List[dict] = []
    video_results = []
    host_report: Optional[dict] = None
    host_result = None

    # Videos are deliberately absent from this set: reviews must never be
    # gated on a video platform answering.
    expected_review_sources = {"host"}
    seen_review_sources: set = set()

    reviews: List[dict] = []
    filter_report: Optional[dict] = None
    passed_count = 0

    queue: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def pump_sources() -> None:
        """Forward collector output onto the shared queue."""
        try:
            async for item in collect_streaming(
                product_url=product_url,
                product_name=product_name,
                product_id=product_id,
                site_hint=site_hint,
            ):
                await queue.put(("collected", item))
        except Exception as error:  # pragma: no cover - collector guards itself
            logger.exception("source collection failed")
            await queue.put(("failed", f"{type(error).__name__}: {error}"))
        finally:
            await queue.put(("sources_done", None))

    async def pump_summary(surviving: List[dict], total: int, video_evidence: List[dict]) -> None:
        """Summarize, then put the result on the queue when it is ready."""
        try:
            bundle = await summarize_reviews(
                product_name,
                surviving,
                total_scraped=total,
                video_evidence=video_evidence,
                filter_report=filter_report,
                product_description=(host_report or {}).get("description"),
                product_url=product_url,
            )
        except Exception as error:  # pragma: no cover - summarize guards itself
            logger.exception("summarization failed")
            bundle = SummaryBundle(meta=SummaryResult(provider="none", error=str(error)))
        await queue.put(("summary", bundle))

    def run_filter() -> List[dict]:
        """Filter everything collected so far; returns the surviving reviews."""
        nonlocal reviews, filter_report, passed_count

        collected = list(host_result.reviews if host_result else [])
        reviews = [review.to_dict() for review in collected]
        passed_count = len(reviews)

        if settings.filter_enabled and reviews:
            verdicts, report = filter_reviews(reviews)
            for review, verdict in zip(reviews, verdicts):
                review["filter"] = verdict.to_dict()
            filter_report = report.to_dict()
            passed_count = report.passed
            for verdict, review in zip(verdicts, reviews):
                if not verdict.passed:
                    # Structured, and without the reviewer's identity: the
                    # review text is public, who wrote it is not this
                    # service's business to record.
                    logger.info(
                        "review filtered out",
                        extra={
                            "event_type": "review_filtered",
                            "source": review.get("source"),
                            "score": round(verdict.score, 3),
                            "rules": [signal.rule for signal in verdict.signals],
                            "text_excerpt": (review.get("text") or "")[:80],
                        },
                    )
        elif reviews:
            for review in reviews:
                review["filter"] = {
                    "passed": True, "suspicion_score": 0.0, "confidence": "none",
                    "signals": [], "rules_triggered": [],
                    "note": "filtering disabled (FILTER_ENABLED=false)",
                }

        return [r for r in reviews if (r.get("filter") or {}).get("passed", True)]

    def reviews_event() -> dict:
        return {
            "event": "reviews",
            "reviews": reviews,
            "reviews_passed": passed_count,
            "filter_report": filter_report,
            "sources": list(review_sources),
        }

    def review_stage_events() -> List[dict]:
        """Investigation-trail checkpoints backed by the filtering just run.

        Sentiment and pattern signals are computed by the fake-review filter
        itself (``app.services.nlp.filter``) independently of the LLM, so
        these are real checkpoints over work already done — not new
        detection logic and not gated on summarization.
        """
        events = [
            _stage(
                "reviews_collected", "complete",
                detail=(
                    f"{passed_count} of {len(reviews)} review(s) passed filtering"
                    if reviews else "No reviews were found for this product"
                ),
            )
        ]
        if not reviews:
            events.append(_stage("sentiment_analyzed", "skipped", detail="No reviews to analyze"))
            events.append(_stage("pattern_analysis", "skipped", detail="No reviews to analyze"))
        else:
            events.append(
                _stage("sentiment_analyzed", "complete",
                       detail=f"Sentiment signals scored across {len(reviews)} review(s)")
            )
            if filter_report:
                clusters = len(filter_report.get("duplicate_clusters") or [])
                events.append(
                    _stage(
                        "pattern_analysis", "complete",
                        detail=(
                            f"{clusters} duplicate-phrasing cluster(s) found"
                            if clusters else "No duplicate-phrasing or submission-timing clusters found"
                        ),
                    )
                )
            else:
                events.append(_stage("pattern_analysis", "skipped", detail="Pattern filtering is disabled"))
        return events

    def external_research_event() -> dict:
        """Whether video search turned up anything.

        "External sources" here means the real work this service does today:
        video platforms — not a general web search, which this service does
        not perform.
        """
        attempted_video = len(video_sources) > 0

        if not attempted_video:
            return _stage(
                "external_research", "skipped",
                detail="No product name was available to search video platforms.",
            )

        parts: List[str] = []
        total_videos = sum((v.get("count") or 0) for v in video_sources)
        if total_videos:
            parts.append(f"{total_videos} video(s) across {len(video_sources)} platform(s)")
        else:
            parts.append("no review videos found on the platforms checked")

        return _stage("external_research", "complete" if total_videos else "failed", detail="; ".join(parts) or None)

    sources_task = asyncio.create_task(pump_sources())
    summary_task = None
    summary_bundle = None
    sources_done = False
    external_stage_emitted = False
    reviews_emitted = False
    surviving: List[dict] = []
    failure: Optional[str] = None

    try:
        while not (sources_done and summary_bundle is not None):
            kind, payload = await queue.get()

            if kind == "sources_done":
                sources_done = True

            elif kind == "failed":
                failure = payload

            elif kind == "summary":
                summary_bundle = payload
                summary = {
                    "pros": [], "cons": [], "verdict": "", "confidence": "none", "caveats": [],
                    "trust_score": 50, "star_rating": 2.5,
                    "recommendation": summary_bundle.recommendation,
                    "themes": [], "reasons_to_buy": [], "reasons_to_think_twice": [],
                    "claim_check": None,
                    "who_should_buy": [], "who_should_avoid": [],
                    "alternatives": [], "alternatives_basis": None,
                    "confidence_reason": summary_bundle.confidence_reason,
                    "review_risk": summary_bundle.review_risk,
                    "score_breakdown": summary_bundle.score_breakdown,
                    "evidence": summary_bundle.evidence,
                }
                if summary_bundle.summary is not None:
                    summary = {
                        "pros": summary_bundle.summary.pros,
                        "cons": summary_bundle.summary.cons,
                        "verdict": summary_bundle.summary.verdict,
                        "confidence": summary_bundle.summary.confidence,
                        "caveats": summary_bundle.summary.caveats,
                        "trust_score": summary_bundle.summary.trust_score,
                        "star_rating": summary_bundle.summary.star_rating,
                        "recommendation": summary_bundle.recommendation,
                        "themes": [theme.model_dump() for theme in summary_bundle.summary.themes],
                        "reasons_to_buy": summary_bundle.summary.reasons_to_buy,
                        "reasons_to_think_twice": summary_bundle.summary.reasons_to_think_twice,
                        "claim_check": summary_bundle.summary.claim_check,
                        "who_should_buy": summary_bundle.summary.who_should_buy,
                        "who_should_avoid": summary_bundle.summary.who_should_avoid,
                        "alternatives": [alt.model_dump() for alt in summary_bundle.summary.alternatives],
                        "alternatives_basis": summary_bundle.summary.alternatives_basis,
                        "confidence_reason": summary_bundle.confidence_reason,
                        "review_risk": summary_bundle.review_risk,
                        "score_breakdown": summary_bundle.score_breakdown,
                        "evidence": summary_bundle.evidence,
                    }
                yield {
                    "event": "summary",
                    "summary": summary,
                    "llm": summary_bundle.to_meta_dict(),
                }

                risk = summary_bundle.review_risk or {}
                if risk.get("score") is None:
                    yield _stage(
                        "review_reliability", "skipped",
                        detail=risk.get("note") or "Not enough data to assess review reliability",
                    )
                else:
                    yield _stage(
                        "review_reliability", "complete",
                        detail=f"{risk.get('level')} risk ({len(risk.get('signals') or [])} signal(s) observed)",
                    )

                if summary_bundle.summary is None:
                    reason = summary_bundle.meta.error or "No verdict could be produced from the available evidence."
                    yield _stage("claim_research", "skipped", detail=reason)
                    yield _stage("evidence_synthesis", "failed", detail=reason)
                    yield _stage("trust_score", "failed", detail=reason)
                    yield _stage("verdict", "failed", detail=reason)
                else:
                    output = summary_bundle.summary
                    if output.claim_check:
                        yield _stage("claim_research", "complete", detail=output.claim_check)
                    else:
                        yield _stage(
                            "claim_research", "skipped",
                            detail="No product description was available to cross-check against reviews.",
                        )
                    evidence_items = summary_bundle.evidence or []
                    concerns = sum(1 for item in evidence_items if item.get("type") == "concern")
                    conflicts = sum(1 for item in evidence_items if item.get("type") == "claim_conflict")
                    yield _stage(
                        "evidence_synthesis", "complete",
                        detail=(
                            f"{len(evidence_items)} evidence item(s) found "
                            f"({concerns} concern(s), {conflicts} claim conflict(s))"
                            if evidence_items else "Limited evidence synthesized"
                        ),
                    )
                    breakdown = summary_bundle.score_breakdown or {}
                    yield _stage(
                        "trust_score", "complete",
                        detail=breakdown.get("explanation") or f"{output.trust_score}/100 ({output.confidence} confidence)",
                    )
                    yield _stage("verdict", "complete", detail=output.verdict or None)

            elif kind == "collected":
                source_kind, value = payload

                if source_kind == "done":
                    pass  # the collector's own assembled Collection; not needed here

                elif source_kind == "host":
                    host_result = value
                    host_report = value.to_dict()
                    details = host_report.get("product_details") or None
                    if details and details.get("price") is not None and details.get("currency"):
                        try:
                            converted = await usd_amount(details["price"], details["currency"])
                        except Exception:  # pragma: no cover - never let this break the pipeline
                            logger.exception("currency conversion failed")
                            converted = None
                        if converted is not None:
                            details["price_usd"] = converted
                    review_sources.append(host_report)
                    seen_review_sources.add("host")
                    yield {"event": "source", "kind": "host", "report": host_report}
                    if host_report.get("blocked"):
                        yield _stage(
                            "product_info_collected", "failed",
                            detail=f"{host_report.get('source')} blocked the request",
                        )
                    elif host_report.get("image_url") or host_report.get("description"):
                        yield _stage("product_info_collected", "complete", detail="Image and/or description found")
                    elif not product_url:
                        yield _stage(
                            "product_info_collected", "skipped",
                            detail="No product URL was supplied, so there was no page to read",
                        )
                    elif host_report.get("error"):
                        yield _stage(
                            "product_info_collected", "failed",
                            detail=host_report["error"],
                        )
                    else:
                        yield _stage(
                            "product_info_collected", "complete",
                            detail="Product page reached; no image or description found",
                        )

                elif source_kind == "video":
                    video_sources.append(value.to_dict())
                    video_results.append(value)
                    yield {
                        "event": "videos",
                        "source": value.source,
                        "report": value.to_dict(),
                        "videos": merged_videos([value]),
                    }

            # Reviews are ready as soon as every review source has reported —
            # tell the frontend immediately either way, so "no reviews found"
            # never looks like "still searching".
            if not reviews_emitted and seen_review_sources >= expected_review_sources:
                surviving = run_filter()
                yield reviews_event()
                for stage_event in review_stage_events():
                    yield stage_event
                reviews_emitted = True
                if surviving:
                    # Real review evidence: summarize now, in parallel with
                    # whatever video lookups are still outstanding.
                    summary_task = asyncio.create_task(pump_summary(surviving, len(reviews), []))
                # else: nothing to summarize yet — wait for video collection to
                # finish (below) so the fallback verdict has video evidence to
                # draw on, instead of summarizing on reviews alone (=none).

            # Every source has finished, including videos: if nothing started
            # the summary above (no surviving reviews), fall back to whatever
            # video evidence was found rather than skipping the stage in silence.
            if sources_done and not external_stage_emitted:
                yield external_research_event()
                external_stage_emitted = True

            if summary_task is None and sources_done:
                if not reviews_emitted:
                    surviving = run_filter()
                    yield reviews_event()
                    for stage_event in review_stage_events():
                        yield stage_event
                    reviews_emitted = True
                else:
                    surviving = []
                video_evidence = [
                    {
                        "title": v.get("title"),
                        "channel": v.get("channel"),
                        "views": v.get("views"),
                        "published": v.get("published"),
                        "description": v.get("description"),
                    }
                    for v in merged_videos(video_results)[:6]
                ]
                summary_task = asyncio.create_task(
                    pump_summary(surviving, len(reviews), video_evidence)
                )
    finally:
        if not sources_task.done():
            sources_task.cancel()

    if failure:
        yield {"event": "error", "message": failure}

    contributed = [r["source"] for r in review_sources if r.get("count")]
    contributed += [r["source"] for r in video_sources if r.get("count")]

    enough = passed_count >= settings.min_reviews
    status = "ok" if enough else "not_enough_data"
    message = (
        None if enough
        else _explain(host_report, passed_count, settings.min_reviews, len(reviews) - passed_count)
    )

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "analyze -> status=%s reviews=%s (%s passed) videos=%s summary=%s in %sms",
        status, len(reviews), passed_count, sum(len(v.videos) for v in video_results),
        "yes" if (summary_bundle and summary_bundle.summary) else "no", duration_ms,
    )

    # Partial: real evidence was produced, but some real source failed along
    # the way. Computed here from the same reports the UI already carries —
    # never inferred from timing or guessed.
    partial_reasons: List[str] = []
    if enough:
        for report in review_sources:
            if report.get("source") != (host_report or {}).get("source") and (report.get("blocked") or report.get("error")):
                partial_reasons.append(f"{report.get('source')} reviews were unavailable ({report.get('error') or 'blocked'})")
        if (host_report or {}).get("blocked") or (host_report or {}).get("error"):
            partial_reasons.append(f"{(host_report or {}).get('source')} partially blocked review collection")
        for vreport in video_sources:
            if vreport.get("error"):
                partial_reasons.append(f"{vreport.get('source')} video search failed ({vreport['error']})")
        if summary_bundle and not summary_bundle.meta.ok and summary_bundle.summary is None:
            partial_reasons.append(summary_bundle.meta.error or "AI summarization was unavailable")
    partial = enough and bool(partial_reasons)

    yield {
        "event": "done",
        "status": status,
        "message": message,
        "contributed": contributed,
        "video_sources": video_sources,
        "videos": merged_videos(video_results),
        "duration_ms": duration_ms,
        "image_url": (host_report or {}).get("image_url"),
        "description": (host_report or {}).get("description"),
        "product_details": (host_report or {}).get("product_details"),
        "partial": partial,
        "partial_reasons": partial_reasons,
    }


async def analyze_once(**kwargs) -> Dict:
    """Run the pipeline and assemble one complete response.

    Consumes the same generator the streaming endpoint forwards, so the two
    endpoints cannot disagree about what analysis means.
    """
    assembled: Dict = {
        "status": "ok",
        "summary": {
            "pros": [], "cons": [], "verdict": "", "confidence": "none", "caveats": [],
            "trust_score": 50, "star_rating": 2.5, "recommendation": "INSUFFICIENT_DATA",
            "themes": [], "reasons_to_buy": [], "reasons_to_think_twice": [],
            "claim_check": None, "review_risk": None, "score_breakdown": None, "evidence": [],
            "who_should_buy": [], "who_should_avoid": [],
            "alternatives": [], "alternatives_basis": None, "confidence_reason": None,
        },
        "reviews": [],
        "videos": [],
        "sources": [],
        "video_sources": [],
        "product_match": None,
        "contributed": [],
        "filter_report": None,
        "reviews_passed": 0,
        "llm": None,
        "message": None,
        "image_url": None,
        "description": None,
        "product_details": None,
        "partial": False,
        "partial_reasons": [],
    }

    async for event in analyze_stream(**kwargs):
        name = event.get("event")
        if name == "reviews":
            assembled["reviews"] = event["reviews"]
            assembled["reviews_passed"] = event["reviews_passed"]
            assembled["filter_report"] = event["filter_report"]
            assembled["sources"] = event["sources"]
        elif name == "summary":
            assembled["summary"] = event["summary"]
            assembled["llm"] = event["llm"]
        elif name == "match":
            assembled["product_match"] = event["product_match"]
        elif name == "done":
            assembled["status"] = event["status"]
            assembled["message"] = event["message"]
            assembled["contributed"] = event["contributed"]
            assembled["video_sources"] = event["video_sources"]
            assembled["videos"] = event["videos"]
            assembled["image_url"] = event.get("image_url")
            assembled["description"] = event.get("description")
            assembled["product_details"] = event.get("product_details")
            assembled["partial"] = event.get("partial", False)
            assembled["partial_reasons"] = event.get("partial_reasons", [])
        elif name == "error":
            assembled["status"] = "error"
            assembled["message"] = event.get("message")

    return assembled


def sse(event: dict) -> str:
    """One event, framed for text/event-stream.

    The event name is carried in the JSON rather than an SSE ``event:`` field:
    the popup reads this with fetch + a stream reader (EventSource cannot POST),
    so a single named channel keeps the client parser simple.
    """
    return f"data: {json.dumps(event, separators=(',', ':'), default=str)}\n\n"
