"""Trust Score: a weighted sum of six named, independently computed signals.

This is the deliberate alternative to trusting the LLM's own trust_score
number. Each signal below is computed here, from data already collected —
ratings, the deterministic Review Risk score, the structured evidence list,
cross-platform ratings, and corroboration counts — never from the model's
opinion of its own confidence. A signal that cannot be computed (too little
data) is reported as ``None`` ("Not enough data") and excluded from the
weighted average, whose weight is then redistributed across the remaining
signals rather than silently guessing a value for it.

Every evidence item that contributed to a penalty or a boost carries a
signed ``impact_points`` back-computed from the *same* math that produced
the component it fed — so "recurring complaints: 42/100" and the three
concern items under it are traceable to each other, not two independent
numbers that happen to be shown together.
"""

from typing import Dict, List, Optional

from app.config import settings

NOT_ENOUGH_DATA = "Not enough data"

_REVIEW_VOLUME_TARGET = 12  # mirrors settings.llm_confident_min_reviews as the "full mark"


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _note(value: Optional[float], detail: str) -> str:
    return NOT_ENOUGH_DATA if value is None else detail


# ------------------------------------------------------------------ signals


def _customer_experience(reviews: List[dict]) -> Optional[float]:
    """Overall sentiment, read from star ratings — cheaper and more honest
    than asking a model to re-derive a number it already gave us as pros/cons.
    """
    ratings = [r["rating"] for r in reviews if isinstance(r.get("rating"), (int, float))]
    if not ratings:
        return None
    return _clamp(sum(ratings) / len(ratings) / 5 * 100)


def _review_reliability(review_risk: dict) -> Optional[float]:
    """Inverse of Review Risk (app.services.risk) — already deterministic."""
    score = (review_risk or {}).get("score")
    if score is None:
        return None
    return _clamp(100 - score)


def _evidence_weight(item: dict) -> float:
    """How much one evidence item should count: severity, floored so a real
    but rarely-mentioned point still counts a little, scaled up by how many
    reviews actually raise it."""
    severity = _clamp01(item.get("severity", 0.5))
    mentions = item.get("review_mentions") or 0
    return severity * (0.4 + 0.6 * min(1.0, mentions / 5.0))


def _evidence_penalty(evidence: List[dict], type_filter: str) -> float:
    return sum(_evidence_weight(item) for item in evidence if item.get("type") == type_filter)


def _recurring_complaints(evidence: List[dict], review_count: int) -> Optional[float]:
    if review_count <= 0:
        return None
    penalty = _evidence_penalty(evidence, "concern")
    # Each "full weight" concern (severity 1, mentioned by 5+ reviews) costs
    # up to 25 points; several genuine recurring complaints can still bottom
    # this out, one minor one barely moves it.
    return _clamp(100 - penalty * 25)


def _claim_consistency(evidence: List[dict], claim_attempted: bool) -> Optional[float]:
    if not claim_attempted:
        return None
    penalty = _evidence_penalty(evidence, "claim_conflict")
    # A confirmed claim conflict is a sharper signal than a generic
    # complaint — weighted heavier per item.
    return _clamp(100 - penalty * 35)


def _external_evidence(
    ratings_by_source: Optional[Dict[str, float]], video_count: int, competitor_present: bool
) -> Optional[float]:
    rated = {source: value for source, value in (ratings_by_source or {}).items() if value}
    consistency = None
    if len(rated) >= 2:
        spread = max(rated.values()) - min(rated.values())
        consistency = _clamp(100 - spread * 40)

    corroboration_count = (video_count or 0) + (1 if competitor_present else 0)
    corroboration = _clamp(min(100, corroboration_count * 25)) if corroboration_count > 0 else None

    parts = [value for value in (consistency, corroboration) if value is not None]
    if not parts:
        return None
    return sum(parts) / len(parts)


def _evidence_confidence(review_count: int, source_count: int) -> Optional[float]:
    """How much data this whole analysis rests on — low even with glowing
    sentiment if there are only a few reviews from one platform."""
    if review_count <= 0:
        return None
    volume = _clamp(min(100, review_count / _REVIEW_VOLUME_TARGET * 100))
    diversity = _clamp(min(100, source_count * 50))  # 1 platform -> 50, 2+ -> 100
    return volume * 0.7 + diversity * 0.3


# ------------------------------------------------------------- distribution


def _distribute_impact(evidence: List[dict], type_filter: str, total_points: float) -> None:
    """Split a component's point swing across the evidence items that
    produced it, proportional to each item's own weight. Mutates ``evidence``
    in place, setting ``impact_points`` on every item of this type (0 when
    the component itself could not be computed)."""
    matches = [item for item in evidence if item.get("type") == type_filter]
    if not matches:
        return
    if not total_points:
        for item in matches:
            item["impact_points"] = 0
        return
    weights = [_evidence_weight(item) for item in matches]
    weight_total = sum(weights) or 1.0
    for item, weight in zip(matches, weights):
        item["impact_points"] = round(total_points * (weight / weight_total))


# ------------------------------------------------------------------- output


def _weighted_overall(components: List[dict]) -> int:
    """Weighted average over components that have a value, renormalized so
    missing signals don't silently drag the score toward zero — their
    weight is redistributed across whatever could actually be computed."""
    weight_sum = sum(c["weight"] for c in components if c["value"] is not None)
    if weight_sum <= 0:
        return 50  # nothing to go on at all; genuine uncertainty, not a guess
    score_sum = sum(c["value"] * c["weight"] for c in components if c["value"] is not None)
    return round(score_sum / weight_sum)


def explain_score(components: List[dict], overall: int) -> str:
    """A short, traceable sentence naming the specific signals that moved
    the score — never generic boilerplate."""
    scored = [c for c in components if c["value"] is not None]
    if not scored:
        return "Trust score defaulted to a neutral 50 — no signal had enough evidence to compute."

    for c in scored:
        c["_contribution"] = (c["value"] - 50) * c["weight"]
    positives = sorted((c for c in scored if c["_contribution"] > 0), key=lambda c: -c["_contribution"])
    negatives = sorted((c for c in scored if c["_contribution"] < 0), key=lambda c: c["_contribution"])

    parts = [f"Trust score of {overall}/100."]
    if positives:
        top = ", ".join(f"{c['label'].lower()} ({round(c['value'])})" for c in positives[:2])
        parts.append(f"Pulled up by {top}.")
    if negatives:
        bottom = ", ".join(f"{c['label'].lower()} ({round(c['value'])})" for c in negatives[:2])
        parts.append(f"Pulled down by {bottom}.")
    missing = [c["label"].lower() for c in components if c["value"] is None]
    if missing:
        parts.append(
            f"{', '.join(missing)} could not be assessed (not enough data) and "
            "did not factor into the score."
        )
    return " ".join(parts)


def compute_score_breakdown(
    *,
    reviews: List[dict],
    review_risk: dict,
    evidence: List[dict],
    ratings_by_source: Optional[Dict[str, float]] = None,
    video_count: int = 0,
    competitor_present: bool = False,
    source_count: int = 0,
    claim_attempted: bool = False,
) -> dict:
    """The full breakdown: six named components, the weighted overall score,
    and a traceable explanation. Also mutates ``evidence`` in place, adding
    ``impact_points`` to every item — call this once the evidence list is
    final, since impact points are computed from it.
    """
    review_count = len(reviews)
    concern_count = sum(1 for item in evidence if item.get("type") == "concern")
    conflict_count = sum(1 for item in evidence if item.get("type") == "claim_conflict")

    customer_experience = _customer_experience(reviews)
    review_reliability = _review_reliability(review_risk or {})
    recurring_complaints = _recurring_complaints(evidence, review_count)
    external_evidence = _external_evidence(ratings_by_source, video_count, competitor_present)
    claim_consistency = _claim_consistency(evidence, claim_attempted)
    evidence_confidence = _evidence_confidence(review_count, source_count)

    components = [
        {
            "key": "customer_experience", "label": "Customer experience",
            "value": customer_experience, "weight": settings.score_weight_customer_experience,
            "note": _note(customer_experience, f"Average rating across {review_count} review(s)"),
        },
        {
            "key": "review_reliability", "label": "Review reliability",
            "value": review_reliability, "weight": settings.score_weight_review_reliability,
            "note": _note(review_reliability, f"Review Risk level: {(review_risk or {}).get('level')}"),
        },
        {
            "key": "recurring_complaints", "label": "Recurring complaints",
            "value": recurring_complaints, "weight": settings.score_weight_recurring_complaints,
            "note": _note(recurring_complaints, f"{concern_count} concern(s) identified"),
        },
        {
            "key": "external_evidence", "label": "External evidence",
            "value": external_evidence, "weight": settings.score_weight_external_evidence,
            "note": _note(external_evidence, "Cross-platform ratings and video/competitor corroboration"),
        },
        {
            "key": "claim_consistency", "label": "Claim consistency",
            "value": claim_consistency, "weight": settings.score_weight_claim_consistency,
            "note": (
                _note(claim_consistency, f"{conflict_count} claim conflict(s) found")
                if claim_attempted else "No product description was available to check claims against"
            ),
        },
        {
            "key": "evidence_confidence", "label": "Evidence confidence",
            "value": evidence_confidence, "weight": settings.score_weight_evidence_confidence,
            "note": _note(evidence_confidence, f"{review_count} review(s) across {max(source_count, 1)} platform(s)"),
        },
    ]

    overall = _weighted_overall(components)
    explanation = explain_score(components, overall)
    for component in components:
        component.pop("_contribution", None)

    _distribute_impact(
        evidence, "concern",
        0.0 if recurring_complaints is None else (recurring_complaints - 100) * settings.score_weight_recurring_complaints,
    )
    _distribute_impact(
        evidence, "claim_conflict",
        0.0 if claim_consistency is None else (claim_consistency - 100) * settings.score_weight_claim_consistency,
    )
    _distribute_impact(
        evidence, "evidence",
        0.0 if customer_experience is None else max(0.0, (customer_experience - 50) * settings.score_weight_customer_experience),
    )

    return {"overall": overall, "components": components, "explanation": explanation}


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
