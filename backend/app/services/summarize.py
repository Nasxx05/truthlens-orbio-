"""Review summarization.

Sits between the route and the LLM providers. Responsible for what should not
be delegated to a model:

  * **Deciding what evidence exists.** Only reviews that passed filtering are
    sent, and per-platform average ratings are computed here so the prompt can
    state cross-platform disagreement as fact.
  * **Enforcing caution.** The prompt asks for calibrated confidence, but a
    prompt is a request, not a guarantee. The confidence a model returns is
    capped here against the actual evidence, and the caveats a shopper needs
    are appended whether or not the model thought of them. If the model
    overstates its certainty on eight reviews, the ceiling corrects it.
  * **Degrading gracefully.** No credentials, a refusal, a rate limit or a
    network failure all produce an empty summary with a reason. The endpoint
    still returns its reviews and videos.
"""

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.services.llm import SummaryOutput, SummaryRequest, SummaryResult, get_provider
from app.services.risk import compute_review_risk
from app.services.scoring import compute_recommendation, compute_score_breakdown

logger = logging.getLogger(__name__)

_CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
_RANK_CONFIDENCE = {0: "none", 1: "low", 2: "medium", 3: "high"}


@dataclass
class SummaryBundle:
    """A summary plus how it was produced."""

    summary: Optional[SummaryOutput] = None
    meta: SummaryResult = field(default_factory=SummaryResult)
    adjustments: List[str] = field(default_factory=list)
    review_risk: Optional[dict] = None
    score_breakdown: Optional[dict] = None
    evidence: List[dict] = field(default_factory=list)
    recommendation: str = "INSUFFICIENT_DATA"
    confidence_reason: Optional[str] = None

    def to_meta_dict(self) -> dict:
        data = self.meta.to_dict()
        data["adjustments"] = self.adjustments
        return data


def ratings_by_source(reviews: List[dict]) -> Dict[str, float]:
    """Mean rating per platform, over reviews that carry one."""
    grouped: Dict[str, List[float]] = {}
    for review in reviews:
        rating = review.get("rating")
        source = review.get("source")
        if source and isinstance(rating, (int, float)):
            grouped.setdefault(str(source), []).append(float(rating))
    # Two ratings is not an average worth publishing.
    return {
        source: round(statistics.mean(values), 2)
        for source, values in grouped.items()
        if len(values) >= 3
    }


def _confidence_ceiling(review_count: int, source_count: int) -> str:
    """The highest confidence this much evidence can justify.

    Deliberately strict. A verdict that sounds certain is acted on, and a
    shopper cannot see how many reviews it rested on.
    """
    if review_count == 0:
        # No scraped reviews at all: whatever the verdict rests on (video
        # commentary, general knowledge, or nothing), it is not review evidence.
        return "none"
    if review_count < 5:
        return "low"
    if review_count < settings.llm_confident_min_reviews:
        return "low"
    if source_count < 2:
        # One platform means one moderation policy and one incentive to
        # curate — enough for a qualified view, not a confident one.
        return "medium"
    if review_count < 35:
        return "medium"
    return "high"


def _confidence_reason(confidence: str, review_count: int, source_count: int) -> str:
    """A short, human, traceable explanation of *why* confidence landed where it
    did — the same review_count/source_count numbers that drove
    ``_confidence_ceiling``, restated as a sentence rather than left as a bare
    'low'/'medium'/'high' chip with no visible reasoning.
    """
    if review_count <= 0:
        return "none — no customer reviews were found for this product."
    platform_word = "platform" if source_count == 1 else "platforms"
    basis = f"based on {review_count} review{'s' if review_count != 1 else ''} from {max(source_count, 1)} {platform_word}"
    return f"{confidence} — {basis}."


def _apply_caution(
    summary: SummaryOutput,
    *,
    review_count: int,
    source_count: int,
    sources: List[str],
    filtered_out: int,
    total_scraped: int,
    ratings: Dict[str, float],
) -> Tuple[SummaryOutput, List[str]]:
    """Cap confidence and add the caveats a shopper needs.

    Returns the adjusted summary and a list of what was changed, so the
    correction is visible rather than silent.
    """
    adjustments: List[str] = []

    stated = (summary.confidence or "low").strip().lower()
    if stated not in _CONFIDENCE_RANK:
        adjustments.append(f"model returned unrecognized confidence {stated!r}; treated as low")
        stated = "low"

    ceiling = _confidence_ceiling(review_count, source_count)
    if _CONFIDENCE_RANK[stated] > _CONFIDENCE_RANK[ceiling]:
        adjustments.append(
            f"confidence lowered from {stated!r} to {ceiling!r}: "
            f"{review_count} review(s) from {source_count} platform(s) cannot support more"
        )
        summary.confidence = ceiling
    else:
        summary.confidence = stated

    caveats = list(summary.caveats or [])

    def add(text: str) -> None:
        # Avoid repeating a point the model already made in its own words.
        keys = text.lower().split()[:3]
        if not any(all(key in existing.lower() for key in keys) for existing in caveats):
            caveats.append(text)

    if review_count == 0:
        add(
            "No customer reviews were found for this product — this verdict draws on "
            "video commentary and/or general knowledge instead, not verified reviews."
        )
    elif review_count < settings.llm_confident_min_reviews:
        add(
            f"Based on only {review_count} review(s) — too few to be confident this "
            "reflects most buyers' experience."
        )
    if review_count and source_count < 2:
        add(
            f"All reviews come from a single platform ({sources[0] if sources else 'unknown'}), "
            "so they share whatever moderation and incentives that platform has."
        )
    if filtered_out and total_scraped:
        share = filtered_out / total_scraped
        if share >= 0.3:
            add(
                f"{filtered_out} of {total_scraped} reviews ({share:.0%}) were removed by "
                "automated review-pattern filtering, which is a high share — treat the "
                "remaining reviews with care."
            )

    rated = {source: value for source, value in ratings.items() if value}
    if len(rated) >= 2:
        spread = max(rated.values()) - min(rated.values())
        if spread >= 0.7:
            highest = max(rated, key=lambda s: rated[s])
            lowest = min(rated, key=lambda s: rated[s])
            add(
                f"Ratings disagree across platforms: {highest} averages "
                f"{rated[highest]:.1f}/5 versus {lowest} at {rated[lowest]:.1f}/5."
            )

    summary.caveats = caveats

    # Defensive normalization — never trust the model's raw numbers verbatim.
    try:
        clamped_score = max(0, min(100, round(float(summary.trust_score))))
    except (TypeError, ValueError):
        clamped_score = 50
    if clamped_score != summary.trust_score:
        adjustments.append(f"trust_score normalized to {clamped_score}")
    summary.trust_score = clamped_score

    try:
        clamped_stars = max(0.0, min(5.0, round(float(summary.star_rating) * 2) / 2))
    except (TypeError, ValueError):
        clamped_stars = 2.5
    if clamped_stars != summary.star_rating:
        adjustments.append(f"star_rating normalized to {clamped_stars}")
    summary.star_rating = clamped_stars

    return summary, adjustments


async def summarize_reviews(
    product_name: Optional[str],
    reviews: List[dict],
    *,
    total_scraped: Optional[int] = None,
    provider_name: Optional[str] = None,
    video_evidence: Optional[List[dict]] = None,
    filter_report: Optional[dict] = None,
    product_description: Optional[str] = None,
    product_url: Optional[str] = None,
) -> SummaryBundle:
    """Summarize the reviews that passed filtering.

    ``reviews`` must already be filtered — this function does not re-filter,
    and passing everything would summarize the fakes along with the rest.

    When ``reviews`` is empty, a verdict is still produced — grounded in
    ``video_evidence`` and, failing that, the model's own general knowledge —
    rather than returning nothing. See ``prompt.py``'s fallback prompt.

    ``filter_report`` is the ``FilterReport.to_dict()`` shape produced by
    ``app.services.nlp.filter`` over the *pre-filter* review set, used to
    compute Review Risk — a presentation layer over data already computed
    for filtering, not new detection logic.
    """
    started = time.monotonic()
    total_scraped = len(reviews) if total_scraped is None else total_scraped
    filtered_out = max(0, total_scraped - len(reviews))

    review_risk = compute_review_risk(filter_report, total_scraped)

    bundle = SummaryBundle(meta=SummaryResult(provider="none"), review_risk=review_risk)

    provider = get_provider(provider_name)
    if provider is None:
        bundle.meta.error = "no LLM provider is configured (set LLM_PROVIDER)"
        bundle.meta.duration_ms = int((time.monotonic() - started) * 1000)
        return bundle

    bundle.meta = SummaryResult(provider=provider.name, model=provider.model)

    sources = sorted({str(review.get("source")) for review in reviews if review.get("source")})
    ratings = ratings_by_source(reviews)

    request = SummaryRequest(
        product_name=product_name or "",
        reviews=reviews,
        sources=sources,
        total_scraped=total_scraped,
        filtered_out=filtered_out,
        ratings_by_source=ratings,
        video_evidence=(video_evidence or []) if not reviews else [],
        product_description=product_description,
        product_url=product_url,
    )

    result = await provider.summarize(request)
    bundle.meta = result

    if not result.ok or result.summary is None:
        logger.info("summarization produced nothing: %s", result.error)
        return bundle

    summary, adjustments = _apply_caution(
        result.summary,
        review_count=result.reviews_used or len(reviews),
        source_count=len(sources),
        sources=sources,
        filtered_out=filtered_out,
        total_scraped=total_scraped,
        ratings=ratings,
    )
    bundle.summary = summary
    bundle.adjustments = adjustments
    bundle.confidence_reason = _confidence_reason(summary.confidence, result.reviews_used or len(reviews), len(sources))

    review_count = result.reviews_used or len(reviews)
    competitor_present = "competitor" in sources

    # Evidence items are converted to plain dicts up front: scoring mutates
    # them in place (adding impact_points), and the response carries these
    # dicts directly rather than re-wrapping them in the LLM-facing model.
    evidence = [item.model_dump() for item in summary.evidence]

    breakdown = compute_score_breakdown(
        reviews=reviews,
        review_risk=review_risk,
        evidence=evidence,
        ratings_by_source=ratings,
        video_count=len(video_evidence or []) if not reviews else 0,
        competitor_present=competitor_present,
        source_count=len(sources),
        claim_attempted=bool(request.product_description),
    )
    bundle.score_breakdown = breakdown
    bundle.evidence = evidence

    # The trust score is the weighted signal breakdown above, not the
    # model's own number — same reasoning as the confidence ceiling: a
    # score has to be auditable, not just plausible-sounding. Record the
    # override when it's a real correction, same as any other adjustment.
    model_score = summary.trust_score
    summary.trust_score = breakdown["overall"]
    if abs(model_score - breakdown["overall"]) >= 10:
        adjustments.append(
            f"trust_score recomputed from named signals: {model_score} (model) -> "
            f"{breakdown['overall']} (weighted breakdown)"
        )
    summary.star_rating = max(0.0, min(5.0, round(breakdown["overall"] / 100 * 5 * 2) / 2))

    if adjustments:
        logger.info("summary adjusted: %s", "; ".join(adjustments))

    bundle.recommendation = compute_recommendation(
        trust_score=summary.trust_score,
        confidence=summary.confidence,
        review_count=review_count,
        review_risk=review_risk,
    )

    return bundle
