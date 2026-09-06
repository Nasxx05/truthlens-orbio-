"""Prompt construction for review summarization.

Kept separate from the providers so the same prompt is used whichever vendor is
configured — otherwise "swap the provider" quietly means "change the output",
and comparing providers becomes meaningless.

Three things this module is responsible for:

  * **Grounding.** The model is given reviews and told to describe only what is
    in them. A summary that invents a drawback nobody reported is worse than no
    summary, because it looks like evidence.
  * **Calibration.** Confidence is not left to the model's mood. The evidence
    is characterized in the prompt — how many reviews, from how many platforms,
    how much was filtered out, whether platforms disagree — and the model is
    told what each situation means. The caller additionally enforces a ceiling
    in code, because a prompt is a request, not a guarantee.
  * **Budget.** Reviews are capped and truncated so a 200-review product cannot
    produce an enormous request. Selection is spread across sources and ratings
    rather than taking the first N, which on a "most recent" scrape would be
    all one week and on a rating-sorted one all five stars.
"""

import logging
from typing import Dict, List, Tuple

from app.config import settings
from app.services.llm.base import SummaryRequest

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You summarize product reviews for online shoppers deciding whether to buy.

You will be given reviews collected from one or more shopping platforms, along with a
description of how much evidence there is. Reviews that failed automated fake-review
filtering have already been removed before they reach you.

Rules:

1. Ground everything in the supplied reviews. Every pro and con must be something
   reviewers actually said. Never add a point from general knowledge about the product
   or its category, and never infer a fault nobody reported.
2. Prefer points that more than one reviewer raises. A single complaint is worth
   mentioning only if it is serious (safety, failure, misrepresentation) — say that it
   was one reviewer.
3. Be specific. "Battery lasts around 30 hours with noise cancelling on" is useful;
   "good battery life" is not. Use reviewers' own concrete details: durations,
   measurements, what broke, what they compared it to.
4. Do not balance for the sake of balance. If reviewers report almost no problems,
   return few cons. If the product is bad, say so.
5. When reviews come from more than one platform and they disagree, say so explicitly
   in the verdict and in caveats. A product rated far higher on the site selling it
   than elsewhere is a warning worth surfacing.
6. Calibrate confidence to the evidence you were given, and never overstate it. With
   thin evidence, write a genuinely qualified verdict: say what the reviews suggest,
   say plainly that it is not enough to be sure, and do not recommend or reject the
   product as though it were.
7. Write for a shopper, not a marketer. No sales language, no exclamation marks, no
   restating the product name back at them."""


# How the evidence is described to the model, and what each tier licenses. The
# thresholds are deliberate: below ~10 reviews you cannot distinguish a pattern
# from a coincidence, and the verdict has to say so.
def evidence_tier(review_count: int, source_count: int) -> Tuple[str, str]:
    """Classify the evidence and state what a verdict may claim from it."""
    if review_count < 5:
        return (
            "very thin",
            "This is far too little evidence for a verdict. Describe only what these few "
            "reviewers said, state clearly that a handful of reviews cannot support a "
            "recommendation, and set confidence to 'low'.",
        )
    if review_count < 12:
        return (
            "thin",
            "This is a small sample. Report what these reviewers said, avoid implying a "
            "general pattern, note explicitly that the sample is small, and set confidence "
            "to 'low'.",
        )
    if review_count < 35 or source_count < 2:
        return (
            "moderate",
            "Enough for a qualified view. Note in caveats that the evidence is limited"
            + (" and comes from a single platform" if source_count < 2 else "")
            + ". Confidence should be 'medium' at most.",
        )
    return (
        "reasonable",
        "Enough for a confident view on points that recur across reviews. Confidence may "
        "be 'high' only where reviewers clearly agree.",
    )


def _select(reviews: List[dict], limit: int) -> List[dict]:
    """Pick which reviews to send when there are more than the budget allows.

    Spread across source and rating rather than taking the first N. A scrape
    sorted by "most recent" would otherwise send one week of opinion, and a
    rating-sorted one would send nothing but five stars — either way the
    summary describes the sort order, not the product.
    """
    if len(reviews) <= limit:
        return list(reviews)

    buckets: Dict[Tuple[str, int], List[dict]] = {}
    for review in reviews:
        rating = review.get("rating")
        # Group unrated reviews together rather than dropping them.
        band = int(rating) if isinstance(rating, (int, float)) else 0
        buckets.setdefault((str(review.get("source") or "?"), band), []).append(review)

    # Round-robin across buckets so every source and rating band is represented.
    selected: List[dict] = []
    order = sorted(buckets)
    index = 0
    while len(selected) < limit and any(buckets[key] for key in order):
        for key in order:
            if not buckets[key]:
                continue
            selected.append(buckets[key].pop(0))
            if len(selected) >= limit:
                break
        index += 1
        if index > limit:
            break

    logger.debug(
        "selected %s of %s reviews across %s source/rating buckets",
        len(selected), len(reviews), len(order),
    )
    return selected


def _format_review(index: int, review: dict, max_chars: int) -> str:
    """One review, as compact labelled text.

    Plain text rather than JSON: it is less to tokenize, and the model does not
    need to parse structure to read a review.
    """
    bits = [f"[{index}]"]
    source = review.get("source")
    if source:
        bits.append(f"platform={source}")
    rating = review.get("rating")
    if rating is not None:
        bits.append(f"rating={rating}/5")
    date = review.get("date")
    if date:
        bits.append(f"date={date}")
    if review.get("verified_purchase") is True:
        bits.append("verified")

    text = " ".join(str(review.get("text") or "").split())
    if len(text) > max_chars:
        # Mark the cut so the model does not read a severed sentence as the
        # reviewer trailing off.
        text = text[:max_chars].rsplit(" ", 1)[0] + " […truncated]"

    title = " ".join(str(review.get("title") or "").split())
    header = " ".join(bits)
    if title:
        return f"{header}\n{title}\n{text}"
    return f"{header}\n{text}"


def divergence_note(ratings_by_source: Dict[str, float]) -> str:
    """Describe cross-platform rating disagreement, if any.

    This is the signal the whole project exists to surface, so it is stated as
    a fact in the prompt rather than left for the model to notice.
    """
    rated = {source: value for source, value in (ratings_by_source or {}).items() if value}
    if len(rated) < 2:
        return ""

    listed = ", ".join(f"{source} {value:.1f}/5" for source, value in sorted(rated.items()))
    spread = max(rated.values()) - min(rated.values())

    if spread >= 0.7:
        highest = max(rated, key=lambda s: rated[s])
        lowest = min(rated, key=lambda s: rated[s])
        return (
            f"Average rating by platform: {listed}. These disagree by {spread:.1f} stars "
            f"({highest} rates it notably higher than {lowest}). Treat this gap as a "
            "finding: mention it in the verdict and in caveats."
        )
    return f"Average rating by platform: {listed}. The platforms broadly agree ({spread:.1f} stars apart)."


def build_user_prompt(request: SummaryRequest) -> Tuple[str, int]:
    """The user message for a summarization request.

    Returns ``(prompt, reviews_included)``.
    """
    selected = _select(request.reviews, settings.llm_max_reviews)
    sources = sorted({str(review.get("source")) for review in selected if review.get("source")})
    tier, instruction = evidence_tier(len(selected), len(sources))

    lines: List[str] = [
        f"Product: {request.product_name or 'unknown product'}",
        "",
        "## Evidence",
        f"- Reviews supplied: {len(selected)}",
    ]

    if len(request.reviews) > len(selected):
        lines.append(
            f"- (a further {len(request.reviews) - len(selected)} passed filtering but were "
            "not included, to bound request size; those supplied were sampled across "
            "platforms and ratings)"
        )
    if request.filtered_out:
        lines.append(
            f"- {request.filtered_out} of {request.total_scraped} scraped reviews were "
            "removed by fake-review filtering before this point"
        )

    lines.append(f"- Platforms: {', '.join(sources) if sources else 'unknown'}")
    lines.append(f"- Evidence strength: {tier}")

    note = divergence_note(request.ratings_by_source)
    if note:
        lines.append(f"- {note}")

    lines += [
        "",
        f"## What this evidence supports",
        instruction,
        "",
        "## Reviews",
        "",
    ]

    for position, review in enumerate(selected, start=1):
        lines.append(_format_review(position, review, settings.llm_review_chars))
        lines.append("")

    lines += [
        "---",
        "",
        "Summarize these reviews as pros, cons and a verdict, following the rules you "
        "were given. Ground every point in the reviews above.",
    ]

    return "\n".join(lines), len(selected)
