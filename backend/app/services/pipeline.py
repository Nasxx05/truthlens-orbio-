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
from app.services.nlp import filter_reviews
from app.services.llm import SummaryResult
from app.services.summarize import SummaryBundle, summarize_reviews

logger = logging.getLogger(__name__)


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
    ``match``     competitor product-match verdict
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
    # nothing to search competitor sites or video platforms with. Falling
    # back to a name guessed from the URL slug means a blocked host still
    # produces output, instead of "not enough data" with nothing tried.
    if not (product_name or "").strip():
        product_name = derive_name_from_url(product_url)

    yield {
        "event": "started",
        "stages": ["reviews", "videos", "summary"],
        "product": {"name": product_name, "url": product_url, "id": product_id},
    }

    review_sources: List[dict] = []
    video_sources: List[dict] = []
    video_results = []
    host_report: Optional[dict] = None
    host_result = None
    competitor_result = None

    # Videos are deliberately absent from this set: reviews must never be
    # gated on a video platform answering.
    expected_review_sources = {"host"}
    if (product_name or "").strip():
        expected_review_sources.add("competitor")
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
                product_name, surviving, total_scraped=total, video_evidence=video_evidence
            )
        except Exception as error:  # pragma: no cover - summarize guards itself
            logger.exception("summarization failed")
            bundle = SummaryBundle(meta=SummaryResult(provider="none", error=str(error)))
        await queue.put(("summary", bundle))

    def run_filter() -> List[dict]:
        """Filter everything collected so far; returns the surviving reviews."""
        nonlocal reviews, filter_report, passed_count

        collected = list(host_result.reviews if host_result else [])
        if competitor_result:
            collected.extend(competitor_result.reviews)
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

    sources_task = asyncio.create_task(pump_sources())
    summary_task = None
    summary_bundle = None
    sources_done = False
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
                    }
                yield {
                    "event": "summary",
                    "summary": summary,
                    "llm": summary_bundle.to_meta_dict(),
                }

            elif kind == "collected":
                source_kind, value = payload

                if source_kind == "done":
                    pass  # the collector's own assembled Collection; not needed here

                elif source_kind == "host":
                    host_result = value
                    host_report = value.to_dict()
                    review_sources.append(host_report)
                    seen_review_sources.add("host")
                    yield {"event": "source", "kind": "host", "report": host_report}

                elif source_kind == "competitor":
                    competitor_result, verdict, matched = value
                    report = competitor_result.to_dict()
                    review_sources.append(report)
                    seen_review_sources.add("competitor")
                    yield {"event": "source", "kind": "competitor", "report": report}
                    if verdict is not None:
                        yield {
                            "event": "match",
                            "product_match": {
                                **verdict.to_dict(),
                                "matched_title": (matched or {}).get("title"),
                                "matched_url": (matched or {}).get("url"),
                                "matched_site": competitor_result.source,
                            },
                        }

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
            if summary_task is None and sources_done:
                if not reviews_emitted:
                    surviving = run_filter()
                    yield reviews_event()
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

    yield {
        "event": "done",
        "status": status,
        "message": message,
        "contributed": contributed,
        "video_sources": video_sources,
        "videos": merged_videos(video_results),
        "duration_ms": duration_ms,
        "image_url": (host_report or {}).get("image_url"),
    }


async def analyze_once(**kwargs) -> Dict:
    """Run the pipeline and assemble one complete response.

    Consumes the same generator the streaming endpoint forwards, so the two
    endpoints cannot disagree about what analysis means.
    """
    assembled: Dict = {
        "status": "ok",
        "summary": {"pros": [], "cons": [], "verdict": "", "confidence": "none", "caveats": []},
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
