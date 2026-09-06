"""Fake-review filtering.

    lexicon.py      word lists: sentiment, stock praise, concrete-detail markers
    text.py         shared tokenization and shingling
    duplication.py  near-duplicate clustering across a review set
    sentiment.py    polarity and specificity of one review
    pacing.py       submission-date burst detection
    filter.py       combines the above into a per-review verdict and report

Every decision is traceable: a review's verdict lists the rules that fired,
their weights, and the measurement behind each.
"""

from app.services.nlp.filter import FilterReport, ReviewVerdict, filter_reviews

__all__ = ["filter_reviews", "ReviewVerdict", "FilterReport"]
