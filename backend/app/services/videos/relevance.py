"""Relevance filtering for video results.

A platform search for "Sony WH-1000XM5" returns plenty that is not a review of
it: unrelated products the channel also covers, compilations, giveaways, music
using the name. Scoring keeps the ones whose titles actually reference the
product and rejects the rest, rather than passing the platform's ranking
through unexamined.
"""

import re
from typing import List, Optional, Tuple

from app.services.matching import model_tokens, tokenize

# Titles that are not a review of the product even when they name it.
_JUNK_PATTERNS = re.compile(
    r"\b(giveaway|win a|free (?:iphone|phone|laptop)|"
    r"asmr|"
    r"lofi|lo-fi|music|song|beat|remix|playlist|"
    r"prank|reaction to|"
    r"how to (?:get|download|hack|crack)|"
    r"fake vs real|replica|clone|knock ?off|"
    r"top \d+ (?:phones|laptops|headphones|gadgets) (?:of|in) \d{4})\b",
    re.I,
)

# Words that signal an actual review, used as a positive nudge rather than a
# requirement — plenty of good reviews have none of them in the title.
_REVIEW_HINTS = re.compile(
    r"\b(review|reviewed|tested|test|hands ?on|comparison|vs\.?|versus|"
    r"unboxing|long ?term|after \d+ (?:days|weeks|months|years)|"
    r"worth it|should you buy|pros and cons|honest)\b",
    re.I,
)


def score_relevance(product_title: str, video_title: str) -> Tuple[float, List[str]]:
    """How likely is this video to be about this product?

    Returns ``(score, reasons)``. Model-number agreement dominates, since it is
    the least ambiguous evidence a title can carry.
    """
    reasons: List[str] = []

    if not (video_title or "").strip():
        return 0.0, ["no title"]

    if _JUNK_PATTERNS.search(video_title):
        return 0.0, ["title matches a non-review pattern"]

    product_tokens = set(tokenize(product_title))
    video_tokens = set(tokenize(video_title))
    if not product_tokens:
        return 0.0, ["no product title to compare against"]

    product_models = set(model_tokens(product_title))
    video_models = set(model_tokens(video_title))

    shared_models = product_models & video_models
    overlap = product_tokens & video_tokens
    # Fraction of the product's own tokens present, not symmetric overlap: a
    # long video title should not be penalized for saying more.
    coverage = len(overlap) / len(product_tokens)

    score = 0.0
    if shared_models:
        score += 0.6
        reasons.append(f"model token: {', '.join(sorted(shared_models))}")
    score += 0.4 * coverage
    if overlap:
        reasons.append(f"covers {coverage:.0%} of product terms")

    if _REVIEW_HINTS.search(video_title):
        score += 0.12
        reasons.append("title reads as a review")

    return max(0.0, min(1.0, score)), reasons


def is_relevant(
    product_title: str,
    video_title: str,
    threshold: float = 0.45,
) -> Tuple[bool, float, Optional[str]]:
    """Keep-or-drop decision for one video.

    Returns ``(keep, score, reason_if_dropped)``.
    """
    score, reasons = score_relevance(product_title, video_title)
    if score >= threshold:
        return True, score, None

    # Positive reasons explain a *keep*; quoting one as the rejection reason
    # reads as a contradiction ("dropped: title reads as a review"). A hard
    # rejection zeroes the score and carries its own reason.
    if score == 0.0 and reasons:
        return False, score, reasons[0]
    return False, score, f"relevance {score:.2f} below threshold {threshold:.2f}"


def parse_iso8601_duration(value: str) -> Optional[int]:
    """Seconds from an ISO-8601 duration such as ``PT12M34S``.

    The YouTube API reports durations in this format; ``search.list`` does not
    return them at all, which is why durations require a second call.
    """
    if not value or not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        value.strip(),
        re.I,
    )
    if not match:
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return int(total) if total > 0 else None
