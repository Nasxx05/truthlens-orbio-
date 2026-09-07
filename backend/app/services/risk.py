"""Review Risk: a deterministic, code-computed read on review-set patterns.

This is presentation and aggregation over data the fake-review filter
(``app.services.nlp.filter``) already computes per review — no new detection
logic lives here. That matters for the same reason ``summarize.py`` enforces
its own confidence ceiling rather than trusting the LLM's stated confidence:
a risk *score* is safer to compute from hard counts than to ask a model to
assess.

Language throughout is deliberately cautious. This module must never claim a
review — or a review set — is "fake". It reports observable patterns
(duplication, submission bursts, filter fail-rate) and lets the shopper draw
their own conclusion, with a disclaimer that is always attached to the result
and must always be shown alongside it.
"""

from typing import Optional

from app.config import settings

DISCLAIMER = (
    "Review Risk is an AI-generated assessment of observable patterns, not a "
    "definitive determination of review authenticity."
)


def _insufficient(reason: Optional[str] = None) -> dict:
    return {
        "score": None,
        "level": "insufficient_data",
        "signals": [],
        "disclaimer": DISCLAIMER,
        "note": reason or "Not enough data to assess review risk.",
    }


def compute_review_risk(filter_report: Optional[dict], review_count: int) -> dict:
    """Aggregate ``FilterReport``-shaped data into a shopper-facing risk read.

    ``filter_report`` is the plain dict produced by ``FilterReport.to_dict()``
    (see ``app.services.nlp.filter``): ``total``, ``passed``, ``failed``,
    ``rule_counts``, ``duplicate_clusters`` (each ``{"size", "review_indexes",
    "sample"}``), ``pacing`` (``{"analyzed", "burst_days", ...}``), ``notes``.
    """
    if review_count < settings.min_reviews:
        return _insufficient("Too few reviews were collected to assess risk reliably.")

    if not filter_report:
        return _insufficient("Review-pattern filtering was not run for this analysis.")

    total = filter_report.get("total") or 0
    if not total:
        return _insufficient("No reviews were available to assess.")

    failed = filter_report.get("failed") or 0
    fail_share = failed / total

    duplicate_clusters = filter_report.get("duplicate_clusters") or []
    clustered_reviews = sum(cluster.get("size", 0) for cluster in duplicate_clusters)
    duplicate_share = min(1.0, clustered_reviews / total)

    pacing = filter_report.get("pacing") or {}
    burst_days = pacing.get("burst_days") or {}
    burst_share = 0.0
    if pacing.get("analyzed") and burst_days:
        burst_share = min(1.0, sum(burst_days.values()) / total)

    raw = fail_share * 0.55 + duplicate_share * 0.30 + burst_share * 0.15
    score = round(max(0.0, min(1.0, raw)) * 100)

    if score >= 60:
        level = "high"
    elif score >= 30:
        level = "medium"
    else:
        level = "low"

    signals = []
    if failed:
        signals.append({
            "label": "Reviews flagged by automated filtering",
            "detail": (
                f"{failed} of {total} reviews ({fail_share:.0%}) showed patterns that "
                "failed automated review-pattern filtering before reaching this analysis."
            ),
        })
    if duplicate_clusters:
        largest = max(duplicate_clusters, key=lambda cluster: cluster.get("size", 0))
        signals.append({
            "label": "Repeated phrasing across reviews",
            "detail": (
                f"Found {len(duplicate_clusters)} group(s) of reviews with unusually "
                f"similar wording, the largest spanning {largest.get('size', 0)} reviews. "
                "This is a pattern worth noting, not proof any individual review is fake."
            ),
        })
    if burst_share > 0:
        signals.append({
            "label": "Unusual submission pattern",
            "detail": (
                f"About {burst_share:.0%} of reviews were posted in short, concentrated "
                "bursts rather than spread out over time."
            ),
        })
    if not signals:
        signals.append({
            "label": "No notable patterns",
            "detail": "No unusual duplication or submission-timing patterns were observed.",
        })

    return {
        "score": score,
        "level": level,
        "signals": signals,
        "disclaimer": DISCLAIMER,
        "note": None,
    }
