"""The review filtering engine.

Combines the detectors into a per-review suspicion score and a pass/fail
verdict, and records *why* — every contributing rule, its weight, and the
measurement behind it — so a decision can be explained rather than trusted.

Design constraints worth stating, because they shape the weights:

  * **No single weak signal can fail a review.** Missing verified-purchase in
    particular is explicitly a contributing signal only: enormous numbers of
    genuine reviews carry no badge, either because the buyer purchased
    elsewhere or because the site never displays one. It is capped so that it
    can never on its own reach the fail threshold, and that cap is enforced in
    code rather than left to weight arithmetic.
  * **Set-level signals are apportioned per review.** Belonging to a cluster of
    nine near-identical reviews is strong evidence about *that* review;
    belonging to a pair is weak.
  * **Filtering annotates, it does not delete.** Every review is returned with
    its verdict attached, and the caller chooses what to pass downstream. A
    filter that silently drops data is impossible to debug and impossible to
    correct.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.services.nlp import duplication, pacing, sentiment

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """One rule's contribution to a review's score."""

    rule: str
    weight: float
    detail: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "weight": round(self.weight, 3), "detail": self.detail}


@dataclass
class ReviewVerdict:
    """Filtering outcome for one review."""

    index: int
    score: float = 0.0                       # 0 = clean, 1 = almost certainly fake
    passed: bool = True
    signals: List[Signal] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.score >= 0.75:
            return "high"
        if self.score >= 0.5:
            return "medium"
        if self.score >= 0.25:
            return "low"
        return "none"

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "suspicion_score": round(self.score, 3),
            "confidence": self.confidence,
            "signals": [signal.to_dict() for signal in self.signals],
            "rules_triggered": [signal.rule for signal in self.signals],
        }


@dataclass
class FilterReport:
    """Set-level filtering summary."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    threshold: float = 0.0
    rule_counts: Dict[str, int] = field(default_factory=dict)
    duplicate_clusters: List[dict] = field(default_factory=list)
    pacing: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "threshold": self.threshold,
            "rule_counts": self.rule_counts,
            "duplicate_clusters": self.duplicate_clusters,
            "pacing": self.pacing,
            "notes": self.notes,
        }


# Maximum contribution each rule can make. They intentionally sum above 1.0:
# a review exhibiting everything should saturate, but no single rule except
# large-cluster duplication should be able to fail a review by itself.
WEIGHTS = {
    "duplicate_phrasing": 0.55,
    # Set just above the default fail threshold so that a review which is
    # *entirely* stock praise fails on its own evidence. Previously it capped
    # below the threshold, which meant an identical contentless review passed
    # or failed depending on whether the site happened to show a
    # verified-purchase badge — letting the weakest signal decide the outcome,
    # which is exactly what it must never do.
    "contentless_praise": 0.52,
    "submission_burst": 0.30,
    "unverified_purchase": 0.12,
}

# Hard cap on how far signals that must never be decisive can push a score.
# Missing verified-purchase plus nothing else must land below any sane
# threshold, whatever the weights are later tuned to.
CONTRIBUTING_ONLY = {"unverified_purchase"}


def _duplication_signal(finding: duplication.DuplicationFinding) -> Optional[Signal]:
    """Weight duplication by cluster size, not just similarity.

    Two similar reviews can be coincidence — there are only so many ways to say
    a battery lasts a long time. Nine near-identical reviews are a template.
    """
    if finding.cluster_size < 2:
        return None

    # 2 members -> 0.45 of the rule's weight, rising to full weight at 6+.
    size_factor = min(1.0, 0.45 + 0.11 * (finding.cluster_size - 2))
    weight = WEIGHTS["duplicate_phrasing"] * size_factor * min(1.0, finding.max_similarity / 0.8)

    return Signal(
        rule="duplicate_phrasing",
        weight=weight,
        detail=(
            f"near-duplicate of {finding.cluster_size - 1} other review(s) "
            f"(peak similarity {finding.max_similarity:.2f}); "
            f"cluster of {finding.cluster_size}"
        ),
    )


def _sentiment_signal(finding: sentiment.SentimentFinding) -> Optional[Signal]:
    """Flag maximal enthusiasm carrying no information."""
    if not sentiment.looks_contentless(finding):
        return None

    # Scale by how egregious the emptiness is. Two routes to full weight:
    # a review made entirely of stock phrases, or a short one that is pure
    # enthusiasm with no concrete content at all. The second route matters
    # because synonym-swapping defeats any fixed phrase list — "outstanding
    # quality plus rapid shipping" is the same empty review as "excellent
    # quality and fast shipping", and a lexicon will always be one synonym
    # behind. Length and absence of detail are measurable regardless of word
    # choice.
    empty_and_brief = (
        0.45 if (finding.word_count <= 15 and finding.specificity == 0.0)
        else 0.20 if finding.word_count <= 25
        else 0.0
    )
    boilerplate_factor = min(1.0, 0.55 + max(finding.generic_ratio, empty_and_brief))
    weight = WEIGHTS["contentless_praise"] * boilerplate_factor

    pieces = [f"polarity {finding.polarity:+.2f}", f"specificity {finding.specificity:.2f}"]
    if finding.generic_phrases:
        pieces.append(f"stock phrases: {', '.join(finding.generic_phrases[:3])}")
    if finding.word_count <= 25:
        pieces.append(f"only {finding.word_count} words")

    return Signal(
        rule="contentless_praise",
        weight=weight,
        detail="uniform praise with no specifics — " + "; ".join(pieces),
    )


def _pacing_signal(finding: pacing.PacingFinding) -> Optional[Signal]:
    """Flag membership of a submission burst."""
    if not finding.in_burst:
        return None
    # A day holding 80% of reviews is worse than one holding 30%.
    weight = WEIGHTS["submission_burst"] * min(1.0, 0.5 + finding.day_share)
    return Signal(
        rule="submission_burst",
        weight=weight,
        detail=f"unnatural submission pacing — {finding.reason}",
    )


def _verification_signal(verified: Optional[bool]) -> Optional[Signal]:
    """Missing verified-purchase badge — contributing evidence only.

    ``None`` means the site never said, which is not the same as the site
    saying the purchase was unverified. Both are treated as the same weak
    signal here because neither can be distinguished from the outside, and the
    weight is small precisely because of that ambiguity.
    """
    if verified is True:
        return None
    return Signal(
        rule="unverified_purchase",
        weight=WEIGHTS["unverified_purchase"],
        detail=(
            "no verified-purchase badge"
            if verified is False
            else "site did not indicate whether the purchase was verified"
        ),
    )


def filter_reviews(
    reviews: Sequence[dict],
    *,
    threshold: Optional[float] = None,
    duplicate_threshold: Optional[float] = None,
) -> tuple:
    """Score every review in a set.

    Takes review dicts (as produced by the scrapers) so this engine stays
    independent of the scraping types. Returns ``(verdicts, report)``.
    """
    threshold = settings.filter_threshold if threshold is None else threshold
    duplicate_threshold = (
        settings.duplicate_similarity if duplicate_threshold is None else duplicate_threshold
    )

    verdicts = [ReviewVerdict(index=i) for i in range(len(reviews))]
    report = FilterReport(total=len(reviews), threshold=threshold)

    if not reviews:
        report.notes.append("no reviews to filter")
        return verdicts, report

    texts = [str(review.get("text") or "") for review in reviews]

    # --- set-level detectors ---
    duplication_findings = duplication.analyze(texts, threshold=duplicate_threshold)

    pacing_findings, pacing_report = pacing.analyze(
        [(i, review.get("date"), review.get("date_raw")) for i, review in enumerate(reviews)],
        min_dated=settings.pacing_min_dated,
    )
    pacing_by_index = {finding.index: finding for finding in pacing_findings}
    report.pacing = {
        "analyzed": pacing_report.analyzed,
        "usable_dates": pacing_report.usable_dates,
        "distinct_days": pacing_report.distinct_days,
        "span_days": pacing_report.span_days,
        "burst_days": pacing_report.burst_days,
        "note": pacing_report.note,
    }
    if pacing_report.note:
        report.notes.append(f"pacing: {pacing_report.note}")

    # --- per-review scoring ---
    for index, review in enumerate(reviews):
        verdict = verdicts[index]
        signals: List[Optional[Signal]] = [
            _duplication_signal(duplication_findings[index]),
            _sentiment_signal(sentiment.analyze(texts[index])),
            _pacing_signal(pacing_by_index.get(index, pacing.PacingFinding(index=index))),
            _verification_signal(review.get("verified_purchase")),
        ]
        verdict.signals = [signal for signal in signals if signal is not None]

        score = sum(signal.weight for signal in verdict.signals)

        # Enforce the contributing-only rule structurally: if everything that
        # fired is a weak signal, the review cannot be failed on it. Relying on
        # weights alone would make this true only until someone retunes them.
        substantive = [s for s in verdict.signals if s.rule not in CONTRIBUTING_ONLY]
        if not substantive:
            score = min(score, threshold - 0.01)

        verdict.score = max(0.0, min(1.0, score))
        verdict.passed = verdict.score < threshold

        for signal in verdict.signals:
            report.rule_counts[signal.rule] = report.rule_counts.get(signal.rule, 0) + 1

    report.passed = sum(1 for verdict in verdicts if verdict.passed)
    report.failed = report.total - report.passed

    # Cluster summary, so a reviewer of the output can see the templates found
    # without reading every review's signals.
    clusters: Dict[int, List[int]] = {}
    for finding in duplication_findings:
        if finding.cluster_size > 1 and finding.cluster_id >= 0:
            clusters.setdefault(finding.cluster_id, []).append(finding.index)
    report.duplicate_clusters = [
        {
            "size": len(members),
            "review_indexes": sorted(members)[:12],
            "sample": texts[sorted(members)[0]][:160],
        }
        for members in sorted(clusters.values(), key=len, reverse=True)[:8]
    ]

    logger.info(
        "filtered %s review(s): %s passed, %s failed (threshold %.2f), rules: %s",
        report.total, report.passed, report.failed, threshold, report.rule_counts,
    )
    return verdicts, report
