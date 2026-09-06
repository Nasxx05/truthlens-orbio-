"""Word lists for review analysis.

Deliberately lexicon-based rather than a trained model. The signals this engine
looks for — template repetition, contentless praise, submission bursts — are
structural, and a lexicon keeps every decision inspectable: a review is flagged
because of specific words and measurements that can be printed in a report,
not because a black box scored it. It also keeps the service dependency-light
and fast enough to run inline on 200 reviews.

Word lists are lowercase and matched on token boundaries.
"""

from typing import FrozenSet

# Strong positive sentiment. Uniform use of these with no specifics is the
# signature of generated praise.
POSITIVE: FrozenSet[str] = frozenset({
    "amazing", "awesome", "excellent", "fantastic", "perfect", "outstanding",
    "superb", "brilliant", "wonderful", "incredible", "flawless", "exceptional",
    "great", "good", "love", "loved", "lovely", "happy", "pleased", "satisfied",
    "recommend", "recommended", "best", "impressive", "impressed", "quality",
    "worth", "beautiful", "solid", "reliable", "comfortable", "fast", "easy",
    "delighted", "thrilled", "phenomenal", "stellar", "top-notch", "premium",
})

NEGATIVE: FrozenSet[str] = frozenset({
    "terrible", "awful", "horrible", "poor", "bad", "worst", "useless",
    "disappointing", "disappointed", "broken", "broke", "defective", "faulty",
    "cheap", "flimsy", "waste", "refund", "returned", "return", "avoid",
    "uncomfortable", "annoying", "frustrating", "failed", "fails", "stopped",
    "rattle", "creak", "creaky", "leaked", "leaking", "died", "dead",
    "overpriced", "misleading", "regret", "unreliable", "mediocre", "meh",
})

# Intensifiers and absolutes. Genuine reviews use them; generated praise
# over-uses them relative to its length.
INTENSIFIERS: FrozenSet[str] = frozenset({
    "very", "really", "extremely", "absolutely", "totally", "completely",
    "highly", "super", "so", "incredibly", "definitely", "certainly",
    "truly", "utterly", "hugely", "massively", "insanely", "perfectly",
})

# Phrases that praise without saying anything about the product. Individually
# innocent — in a review that contains nothing else, they are the whole review.
GENERIC_PRAISE = (
    "great product", "good product", "nice product", "excellent product",
    "highly recommend", "would recommend", "i recommend this",
    "works as expected", "works as described", "as described", "as advertised",
    "value for money", "good value", "great value", "worth every penny",
    "exactly what i needed", "exactly what i wanted", "just what i needed",
    "five stars", "5 stars", "ten out of ten", "10/10",
    "very satisfied", "very happy", "so happy", "love it", "love this",
    "great quality", "good quality", "excellent quality", "amazing quality",
    "fast shipping", "fast delivery", "quick delivery", "arrived quickly",
    "will buy again", "would buy again", "buy it", "must have", "must buy",
    "thank you", "thanks seller", "great seller", "a+", "no complaints",
    "does the job", "does what it says", "no issues", "works great",
    "best purchase", "great purchase", "happy with purchase",
    "strongly recommend", "outstanding quality", "superb quality",
    "rapid shipping", "rapid delivery", "prompt delivery", "well packaged",
    "exceeded my expectations", "beyond my expectations", "top quality",
    "excellent item", "superb item", "great item", "nice item",
)

# Concrete-detail markers. Their presence is evidence of lived experience: a
# real user says what they did with the thing, for how long, and what went
# wrong. Their absence in a long, glowing review is the anomaly.
SPECIFIC_MARKERS = (
    # time and use
    "after a week", "after a month", "after two", "after three", "after six",
    "months later", "weeks later", "years later", "so far", "since then",
    "daily", "every day", "every morning", "commute", "on my desk",
    "first thing", "over the weekend",
    # comparison and context
    "compared to", "compared with", "instead of", "upgraded from",
    "replaced my", "my old", "previous model", "the older",
    # problems and caveats, which generated praise omits
    "however", "although", "though", "but ", "downside", "drawback",
    "only issue", "one issue", "the catch", "wish it", "would have liked",
    "not perfect", "on the other hand", "caveat", "except",
    # concrete usage
    "returned it", "contacted support", "warranty", "customer service",
    "instructions", "manual", "assembly", "setup took", "charge", "battery",
    "washed", "cleaned", "installed",
    # A named month or season is a concrete claim about when something
    # happened, and genuine reviews are full of them.
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "last summer", "last winter", "over christmas", "black friday",
)

# Units and measurements: a real reviewer quotes numbers.
UNIT_PATTERN = (
    r"\b\d+(?:\.\d+)?\s*"
    r"(?:hours?|hrs?|minutes?|mins?|days?|weeks?|months?|years?|"
    r"gb|tb|mb|mm|cm|inch(?:es)?|in|ft|feet|oz|ml|l|litres?|liters?|"
    r"lbs?|kg|g|w|watts?|v|volts?|mah|hz|khz|ghz|mp|k|"
    r"quarts?|gallons?|pints?|cups?|degrees?|percent|%|"
    r"dollars?|usd|eur|gbp)\b"
)


def has_any(text: str, phrases) -> list:
    """Which of ``phrases`` appear in ``text``. Text must already be lowercase."""
    return [phrase for phrase in phrases if phrase in text]
