"""Trust Score signal breakdown and recommendation — both computed in code.

Same philosophy as ``summarize._confidence_ceiling``/``_apply_caution``: the
LLM's own trust_score and confidence are useful inputs, but the "why should I
trust this verdict" breakdown and the final recommendation label are
recomputed deterministically from the underlying counts, so they degrade
gracefully ("Not enough data") instead of ever being fabricated.
"""

from typing import Dict, Optional

NOT_ENOUGH_DATA = "Not enough data"

_REVIEW_VOLUME_TARGET = 12  # mirrors settings.llm_confident_min_reviews as the "full mark"


def compute_score_breakdown(
    *,
    trust_score: int,
    confidence: str,
    review_count: int,
    source_count: int,
    ratings_by_source: Optional[Dict[str, float]],
    review_risk: dict,
    video_count: int = 0,
    competitor_present: bool = False,
) -> dict:
    """Four weighted components explaining the trust score, or "Not enough data"."""
    components = []

    if review_count <= 0:
        components.append({
            "key": "review_volume", "label": "Review volume",
            "value": None, "weight": 0.3, "note": NOT_ENOUGH_DATA,
        })
    else:
        value = round(min(100, review_count / _REVIEW_VOLUME_TARGET * 100))
        components.append({
            "key": "review_volume", "label": "Review volume", "value": value, "weight": 0.3,
            "note": f"{review_count} review(s) across {max(source_count, 1)} platform(s)",
        })

    rated = {source: value for source, value in (ratings_by_source or {}).items() if value}
    if len(rated) < 2:
        components.append({
            "key": "cross_platform_consistency", "label": "Cross-platform consistency",
            "value": None, "weight": 0.2, "note": NOT_ENOUGH_DATA + " (needs 2+ rated platforms)",
        })
    else:
        spread = max(rated.values()) - min(rated.values())
        value = round(max(0, 100 - spread * 40))
        components.append({
            "key": "cross_platform_consistency", "label": "Cross-platform consistency",
            "value": value, "weight": 0.2,
            "note": f"Ratings span {spread:.1f} stars across platforms",
        })

    if review_risk.get("score") is None:
        components.append({
            "key": "review_reliability", "label": "Review reliability",
            "value": None, "weight": 0.3, "note": NOT_ENOUGH_DATA,
        })
    else:
        value = max(0, 100 - review_risk["score"])
        components.append({
            "key": "review_reliability", "label": "Review reliability", "value": value, "weight": 0.3,
            "note": f"Review Risk level: {review_risk['level']}",
        })

    corroboration = (video_count or 0) + (1 if competitor_present else 0)
    if corroboration <= 0:
        components.append({
            "key": "external_corroboration", "label": "External corroboration",
            "value": None, "weight": 0.2, "note": NOT_ENOUGH_DATA,
        })
    else:
        value = min(100, corroboration * 25)
        components.append({
            "key": "external_corroboration", "label": "External corroboration",
            "value": value, "weight": 0.2,
            "note": (
                f"{video_count} video(s) reviewed"
                + (", competitor data found" if competitor_present else "")
            ),
        })

    return {"components": components, "overall_confidence": confidence}


def compute_recommendation(*, trust_score: int, confidence: str, review_count: int, review_risk: dict) -> str:
    """Deterministic enum: BUY_WITH_CONFIDENCE / BUY_WITH_CAUTION / PROCEED_WITH_CAUTION / AVOID / INSUFFICIENT_DATA."""
    if confidence == "none" or review_count <= 0:
        return "INSUFFICIENT_DATA"

    score = trust_score
    risk_level = review_risk.get("level")
    if risk_level == "high":
        score -= 30
    elif risk_level == "medium":
        score -= 15

    if score >= 75:
        return "BUY_WITH_CONFIDENCE"
    if score >= 55:
        return "BUY_WITH_CAUTION"
    if score >= 35:
        return "PROCEED_WITH_CAUTION"
    return "AVOID"
