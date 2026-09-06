"""Sentiment and specificity analysis for one review.

The target is not "positive reviews" — most genuine reviews are positive. It is
the combination that generated praise produces: maximal enthusiasm carrying no
information. A real five-star review says what the thing did, for how long, and
usually what is still slightly wrong with it. A manufactured one says it is
amazing and recommends it.

So two measurements are taken and only their *combination* is treated as a
signal:

  * **polarity** — how one-sided the sentiment is
  * **specificity** — density of concrete detail: numbers with units, time
    spans, comparisons, named problems, contrast words
"""

import re
from dataclasses import dataclass, field
from typing import List

from app.services.nlp.lexicon import (
    GENERIC_PRAISE,
    INTENSIFIERS,
    NEGATIVE,
    POSITIVE,
    SPECIFIC_MARKERS,
    UNIT_PATTERN,
    has_any,
)
from app.services.nlp.text import normalize, tokens

_UNITS = re.compile(UNIT_PATTERN, re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
# A price the reviewer actually paid is a concrete detail, and a common one:
# "bought on sale for $278" is the opposite of contentless.
_PRICE = re.compile(r"(?:[$£€¥₦₹]\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:dollars?|usd|eur|gbp|pounds?)\b)", re.I)


@dataclass
class SentimentFinding:
    """Sentiment and specificity measurements for one review."""

    polarity: float = 0.0            # -1 (all negative) .. +1 (all positive)
    intensity: float = 0.0           # intensifier density
    specificity: float = 0.0         # 0..1, concrete-detail density
    word_count: int = 0
    generic_phrases: List[str] = field(default_factory=list)
    specific_markers: List[str] = field(default_factory=list)
    # Fraction of the text accounted for by generic praise phrases. High values
    # mean the review is mostly boilerplate.
    generic_ratio: float = 0.0


def analyze(text: str) -> SentimentFinding:
    """Measure sentiment and specificity of one review."""
    normalized = normalize(text)
    words = tokens(normalized)
    finding = SentimentFinding(word_count=len(words))

    if not words:
        return finding

    positive = sum(1 for word in words if word in POSITIVE)
    negative = sum(1 for word in words if word in NEGATIVE)
    intensifiers = sum(1 for word in words if word in INTENSIFIERS)

    if positive or negative:
        finding.polarity = (positive - negative) / (positive + negative)
    finding.intensity = intensifiers / len(words)

    finding.generic_phrases = has_any(normalized, GENERIC_PRAISE)
    finding.specific_markers = has_any(normalized, SPECIFIC_MARKERS)

    generic_words = sum(len(tokens(phrase)) for phrase in finding.generic_phrases)
    finding.generic_ratio = min(1.0, generic_words / len(words))

    # Specificity: count distinct kinds of concrete evidence, then normalize by
    # length so a long review is not credited merely for being long.
    measurements = len(_UNITS.findall(normalized))
    numbers = len(_NUMBER.findall(normalized))
    markers = len(finding.specific_markers)
    prices = len(_PRICE.findall(text or ""))

    evidence = measurements * 2 + markers * 2 + prices * 2 + min(numbers, 4)
    # Ten concrete signals in a review of any length is thoroughly specific.
    finding.specificity = min(1.0, evidence / 10.0)

    return finding


def looks_contentless(finding: SentimentFinding) -> bool:
    """Is this praise with nothing in it?

    Requires *all* of: one-sided positive sentiment, near-zero concrete
    detail, and either heavy boilerplate or brevity. Each condition alone
    describes plenty of genuine reviews — a happy customer writing two lines is
    not a bot — so none of them is treated as sufficient on its own.
    """
    if finding.word_count == 0:
        return False
    strongly_positive = finding.polarity >= 0.8
    no_detail = finding.specificity <= 0.2 and not finding.specific_markers
    boilerplate = finding.generic_ratio >= 0.30 or finding.word_count <= 25
    return strongly_positive and no_detail and boilerplate
